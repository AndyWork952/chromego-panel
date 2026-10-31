# -*- coding: utf-8 -*-
"""
ChromeGo 节点面板 — 本地 Web 工具
拉取免费节点池的槽位配置 -> 新旧对比 -> 生成 v2rayN 可导入的分享链接
  cache/  每个槽位最近一次拉取的原始配置（用于新旧对比）
启动：python server.py  ->  http://127.0.0.1:8765
依赖：仅标准库 + PyYAML
"""
import base64
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

import yaml

WARMING = False  # 冷启动预热中标志（预热线程完成后置 False）
EXT_REFRESHING = False  # 外部订阅源刷新中标志
EXT_ERRORS = {}         # 源 key -> 最近一次失败原因（成功即清除），用于界面显示"该源失效"
EXT_ATTEMPT = {}        # 源 key -> 最近一次拉取尝试的时间戳（失败也记，避免自动刷新每分钟重试）

# 外部公共订阅源（2026-10-31 逐个实测筛出，兼顾协议互补与更新时段）
# mirror 是 jsDelivr CDN 兜底，relay 是 wget.la 反代镜像——国内不开代理也能直连
# 注：Pawdroid 已萎缩到 11 个节点（6 个与现有源重合）不再收录
EXT_SOURCES = {
    # 每日自动更新，hy2 数量稳定（实测 28 个上下）
    "ermaozi":   dict(label="ermaozi",
                      url="https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/v2ray.txt",
                      mirror="https://cdn.jsdelivr.net/gh/ermaozi/get_subscribe@main/subscribe/v2ray.txt",
                      relay="https://wget.la/https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/v2ray.txt"),
    # hy2 大池：319 个链接 / 131 个独立节点，与 ermaozi 仅重合 5 个（2026-10-31 实测）
    "cbusifab":  dict(label="cbusifabcap",
                      url="https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/main/Z.txt",
                      mirror="https://cdn.jsdelivr.net/gh/cbusifabcap/daily_free_vpn@main/Z.txt",
                      relay="https://wget.la/https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/main/Z.txt"),
    # 每 6 小时 sing-box 真连接测试 + 国内探针验证（verified/global 两档，global 量更大）
    "leilao":    dict(label="LeilaoMi真测",
                      url="https://raw.githubusercontent.com/LeilaoMi/AutoMergePublicNodes-Optimized/main/output/global.urls",
                      mirror="https://cdn.jsdelivr.net/gh/LeilaoMi/AutoMergePublicNodes-Optimized@main/output/global.urls",
                      relay="https://wget.la/https://raw.githubusercontent.com/LeilaoMi/AutoMergePublicNodes-Optimized/main/output/global.urls"),
    # 每日更新的大池：2501 节点与现有三源仅重合 8 个，TCP 存活率 18.8% 为各源最高（2026-10-31 实测）
    "aliilapro": dict(label="ALIILAPRO",
                      url="https://raw.githubusercontent.com/ALIILAPRO/v2rayNG-Config/main/sub.txt",
                      mirror="https://cdn.jsdelivr.net/gh/ALIILAPRO/v2rayNG-Config@main/sub.txt",
                      relay="https://wget.la/https://raw.githubusercontent.com/ALIILAPRO/v2rayNG-Config/main/sub.txt"),
}
EXT_KEEP_TCP = 120     # TCP 系（vmess/vless/trojan/ss）：真握手测活后跨源按延迟从低到高保留
EXT_KEEP_HY1 = 20      # hysteria v1：QUIC 走 UDP 无法 TCP 握手测活，免检直收
EXT_KEEP_HY2 = 60      # hysteria2：同上免检直收（用户实测移动宽带对 hy2 兼容最好）
EXT_PROBE_LIMIT = 800  # 每源最多送测的 TCP 链接数，防超大源把刷新拖成几分钟
_EXT_TCP_SCHEMES = ("vmess", "vless", "trojan", "ss")
_EXT_SCHEMES = _EXT_TCP_SCHEMES + ("hysteria", "hysteria2")  # 与面板四个槽位类型对应的协议全集
# 外部源只在一种时机刷新：用户在界面上点「立即更新」按钮

PANEL_DIR = Path(__file__).resolve().parent
CACHE_DIR = PANEL_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

PORT = 8765
GITLAB_TPL = ("https://gitlab.com/free9999/ipupdate/-/raw/master"
              "/backup/img/1/2/ipp/{remote}/{slot}/{filename}")
MIRROR_TPL = ("https://www.67867867.xyz/Alvin9999/PAC/refs/heads/master"
              "/backup/img/1/2/ipp/{remote}/{slot}/{filename}")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------- 网络：本地代理自动探测 ----------
# 代理策略（确定性，不自动探测）：界面上填了地址就用它（先真连验证），没填就走直连 + wget.la 镜像。
# 为什么需要镜像：GitHub raw 和 jsDelivr 在国内直连基本是 TLS 被重置，而 urllib 只认环境变量和
# Windows 系统代理——客户端软件用 TUN 或没开系统代理时它一条都读不到。
PROXY_TEST_HOST = ("raw.githubusercontent.com", 443)
_PROXY = dict(url=None, ts=0.0)
_PROXY_LOCK = threading.Lock()
PROXY_OK_TTL = 600   # 验证通过后 10 分钟内复用，不再重验
PROXY_FAIL_TTL = 90  # 验证不过 90 秒后再试（用户把代理软件开起来后自动接上）
# 用户在界面上手填的代理地址（如 http://127.0.0.1:7890），存 proxy.json、重启不丢、不进仓库。
PROXY_CFG_FILE = PANEL_DIR / "proxy.json"
_user_proxy = ""
try:
    _user_proxy = json.loads(PROXY_CFG_FILE.read_text(encoding="utf-8")).get("proxy", "") if PROXY_CFG_FILE.exists() else ""
except Exception:
    _user_proxy = ""


def normalize_proxy(text: str):
    """把用户输入整理成 http://host:port；不合法返回 (None, 原因)。socks 系不支持（urllib 用不了）。"""
    c = (text or "").strip()
    if not c:
        return "", None  # 空 = 清除配置，回到自动探测
    if "://" not in c:
        c = "http://" + c
    u = urllib.parse.urlparse(c)
    if u.scheme in ("socks5", "socks5h", "socks4"):
        return None, "socks 代理用不了（面板走的是 HTTP 代理）。v2rayN 的 10808、Clash Verge 的 7890 都是混合端口，直接填它们就行"
    if u.scheme not in ("http", "https") or not u.hostname or not u.port:
        return None, "格式大概是 http://127.0.0.1:7890 这样（主机 + 端口）"
    if u.port == PORT:
        return None, "这是面板自己的端口，不能填"
    return f"http://{u.hostname}:{u.port}", None


def _http_connect_ok(host, port, timeout=2.5):
    """这个本地端口能否当 HTTP 代理用。验证要到底：CONNECT 换来 200 只说明代理进程活着，
    它背后的节点可能早就挂了（v2rayN 开着但服务器被封很常见）——所以再借隧道真发一次
    TLS 握手 + HEAD，拿不到 HTTP/ 响应就当它不能用，让位给下一候选（直连 + 镜像兜底）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            th, tp = PROXY_TEST_HOST
            s.sendall(f"CONNECT {th}:{tp} HTTP/1.1\r\nHost: {th}:{tp}\r\n\r\n".encode())
            buf = b""
            while b"\r\n" not in buf and len(buf) < 200:
                chunk = s.recv(1)
                if not chunk:
                    break
                buf += chunk
            if b" 200" not in buf.split(b"\r\n")[0]:
                return False
            with SSL_CTX.wrap_socket(s, server_hostname=th) as t:
                t.sendall(f"HEAD / HTTP/1.1\r\nHost: {th}\r\nConnection: close\r\n\r\n".encode())
                return b"HTTP/" in t.recv(64)
    except Exception:
        return False


def detect_proxy(force=False):
    """返回用户在界面上手填的代理地址，没填或填的连不通就 None。
    故意不做任何自动探测（不扫端口、不读环境变量）：行为要确定——填了就用它，没填就走直连 + 镜像兜底。
    手填的地址也要真连一次验证（TLS 到底），节点挂了同样视为不可用。结果带 TTL 缓存。"""
    now = time.time()
    with _PROXY_LOCK:
        ttl = PROXY_OK_TTL if _PROXY["url"] else PROXY_FAIL_TTL
        if not force and _PROXY["ts"] and now - _PROXY["ts"] < ttl:
            return _PROXY["url"]

        found = None
        if _user_proxy:
            u = urllib.parse.urlparse(_user_proxy)
            if (u.scheme in ("http", "https") and u.hostname and u.port
                    and u.port != PORT and _http_connect_ok(u.hostname, u.port)):
                found = _user_proxy
        _PROXY.update(url=found, ts=now)
        if found:
            print(f"网络：经代理 {found} 访问外网")
        return found


def _open_via(url, timeout, hdrs, proxy):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
                urllib.request.HTTPSHandler(context=SSL_CTX)]
    return urllib.request.build_opener(*handlers).open(urllib.request.Request(url, headers=hdrs), timeout=timeout)


def open_url(url, timeout=25, headers=None):
    """统一的出网入口：探到本机代理就走代理，没探到就直连（忽略环境里可能存在的无关代理）。
    代理中途坏了（进程退出或节点挂了）不能空手而归：清缓存重探一次，还不行就退到纯直连——
    GitHub 类站点直连必被掐，但 wget.la 镜像直连是通的，这条退路得留着。"""
    hdrs = {"User-Agent": "Mozilla/5.0 ChromeGoPanel"}
    hdrs.update(headers or {})
    proxy = detect_proxy()
    try:
        return _open_via(url, timeout, hdrs, proxy)
    except Exception:
        if not proxy:
            raise
        with _PROXY_LOCK:
            _PROXY.update(url=None, ts=0.0)  # 这个代理已不可用，清掉缓存让下次重新探测
        try:
            return _open_via(url, timeout, hdrs, detect_proxy(force=True))
        except Exception:
            return _open_via(url, timeout, hdrs, None)  # 最后兜底：不走代理，直连


def _https_ok(host, port=443, timeout=4):
    """真做一次 TLS 握手 + 发个 HEAD，才算"这个站能访问"。
    只连 TCP 是不够的：GFW 往往放行 TCP 三次握手，然后在 TLS 阶段直接掐掉，
    于是"连得上"却"取不到"，用 connect 判断会得出相反的结论。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with SSL_CTX.wrap_socket(s, server_hostname=host) as t:
                t.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                return b"HTTP/" in t.recv(64)
    except Exception:
        return False


def net_diagnosis():
    """把"全都没拉到"翻译成人话：代理失效 / 源真挂了 / 网络不通，三种原因处理方式完全不同。"""
    proxy = detect_proxy()
    if proxy:
        return f"正在走你填的代理 {proxy}，可能是对方源临时挂了，也可能这个代理的节点不通——等会儿再试，或换个代理地址"
    if _https_ok(PROXY_TEST_HOST[0], PROXY_TEST_HOST[1]):
        return "能直接访问 GitHub，但这次没取到内容，可能作者改了地址，也可能是网络抽风"
    return ("直连和镜像都没拉下来，多半是网络整体不通。如果你的代理软件开着，把它的地址"
            "（比如 http://127.0.0.1:10808 或 7890）填到上面「代理地址」框里再试")

TOOLS = {
    "xray":       dict(label="Xray",        sub="VLESS / REALITY",      config="config.json", remote="xray",       slots=4),
    "clash.meta": dict(label="Clash.Meta",  sub="Hysteria / Hysteria2", config="config.yaml", remote="clash.meta2", slots=6),
    "hysteria":   dict(label="Hysteria v1", sub="QUIC 混淆",           config="config.json", remote="hysteria",   slots=4),
    "hysteria2":  dict(label="Hysteria2",   sub="QUIC",                 config="config.json", remote="hysteria2",  slots=4),
}

# 没有本地记录时按工具典型协议给的兜底徽章（有记录后以实际解析出的协议为准）
TOOL_BADGE = {
    "xray":      ("v2rayN 可导入", "ok"),
    "hysteria2": ("v2rayN 可导入", "ok"),
    "clash.meta": ("v2rayN 可导入", "ok"),
    "hysteria":  ("v2rayN 不支持", "no"),
}


# ---------- 拉取 ----------
def fetch_remote(tool_key: str, slot: int):
    """按 bat 的容错顺序：gitlab 主源 -> 67867867.xyz 镜像。返回 (text, source)。"""
    t = TOOLS[tool_key]
    errors = []
    for source, tpl in (("gitlab", GITLAB_TPL), ("mirror", MIRROR_TPL)):
        url = tpl.format(remote=t["remote"], slot=slot, filename=t["config"])
        try:
            with open_url(url, timeout=20) as r:
                if r.status != 200:
                    raise OSError(f"HTTP {r.status}")
                text = r.read().decode("utf-8", errors="replace")
                if not text.strip():
                    raise OSError("空响应")
                return text, source
        except Exception as e:
            errors.append(f"{source}: {e}")
    raise RuntimeError(net_diagnosis())


# ---------- 缓存 ----------
def cache_file(tool_key: str, slot: int) -> Path:
    ext = ".yaml" if TOOLS[tool_key]["config"].endswith(".yaml") else ".json"
    return CACHE_DIR / f"{tool_key}_ip{slot}{ext}"


def read_cached(tool_key: str, slot: int):
    p = cache_file(tool_key, slot)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None


def write_cache(tool_key: str, slot: int, text: str):
    # 先写临时文件再原子替换，避免预热线程和手动拉取并发写坏同一份缓存
    p = cache_file(tool_key, slot)
    tmp = CACHE_DIR / f"{p.name}.{threading.get_ident()}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ---------- 链接生成小工具 ----------
def _wrap_host(host: str) -> str:
    h = str(host).strip("[]")
    return f"[{h}]" if ":" in h else h


def _hp(host, port):
    return f"{_wrap_host(host)}:{port}"


def _qs(params):
    """urlencode 并丢掉空值（""/None/0），分享链接里不出现空参数。"""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None, 0)})


def _proto_key(protocol):
    """vless+reality+xhttp -> vless，取主协议名用于查客户端支持矩阵"""
    return str(protocol).split("+")[0].split(".")[0]


# 各协议的客户端支持矩阵（截至 v2rayN 7.x 时代的生态）：在集合里=✅，否则=❌
_ALL_CLIENTS = ["v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"]
_ALL_OK = set(_ALL_CLIENTS)
SUPPORT = {
    "vless":       _ALL_OK,
    "vmess":       _ALL_OK,
    "trojan":      _ALL_OK,
    "shadowsocks": _ALL_OK,
    "hysteria2":   _ALL_OK,
    # hysteria v1：sing-box 和 v2rayN 都删了，但 mihomo 和 NekoRay 还活着
    "hysteria":    {"NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）", "ChromeGo 自带 hysteria.exe"},
}


def _clients(protocol):
    """该协议各客户端的支持状态：三件套全量列出，ChromeGo 自带只在协议真有它时出现。"""
    ok = SUPPORT.get(_proto_key(protocol), set())
    out = [dict(name=c, ok=c in ok) for c in _ALL_CLIENTS]
    out += [dict(name=c, ok=True) for c in ok if c not in _ALL_CLIENTS]
    return out


def _v2n(protocol):
    """该协议生成的分享链接能否直接导入 v2rayN（7.x，Xray + sing-box 核心）"""
    p = str(protocol)
    if p.startswith(("vless", "vmess", "trojan", "shadowsocks")) or p == "hysteria2":
        return True, ""
    if p == "hysteria":
        return False, "hysteria v1 已被 v2rayN 移除（仅 hysteria2 支持）"
    return False, "私有协议，无标准链接格式"


def _mk_node(protocol, server, port, auth, sni, insecure, links, summary):
    v2n, note = _v2n(protocol)
    return dict(protocol=protocol, server=server, port=port, auth=auth,
                sni=sni, insecure=insecure, links=links, summary=summary,
                v2n=v2n, v2n_note=note, clients=_clients(protocol))


def _one(url, label):
    return [dict(url=url, name=label)] if url else []


def _digits(s, default=None):
    m = re.search(r"\d+", str(s or ""))
    return m.group(0) if m else default


# ---------- 各协议解析 ----------
def parse_xray(data, label):
    o = data["outbounds"][0]
    v = o["settings"]["vnext"][0]
    u = v["users"][0]
    ss = o.get("streamSettings", {})
    rs = ss.get("realitySettings", {}) if ss.get("security") == "reality" else {}
    xs = ss.get("xhttpSettings", {})
    params = {
        "encryption": u.get("encryption", "none"),
        "security": ss.get("security", "tls"),
        "type": ss.get("network", "tcp"),
        "sni": rs.get("serverName", "") or ss.get("tlsSettings", {}).get("serverName", ""),
        "fp": rs.get("fingerprint", "chrome"),
        "pbk": rs.get("publicKey", ""),
        "sid": rs.get("shortId", ""),
        "path": xs.get("path", ""),
        "mode": xs.get("mode", "auto"),
    }
    qs = _qs(params)
    url = f"vless://{u['id']}@{_hp(v['address'], v['port'])}?{qs}#{urllib.parse.quote(label)}"
    return _mk_node(f"vless+{ss.get('security','tls')}+{ss.get('network','tcp')}",
                    v["address"], v["port"], "UUID " + u["id"][:8] + "…", params["sni"], None,
                    _one(url, label), f"{_hp(v['address'], v['port'])} · SNI {params['sni'] or '—'}")


def parse_hysteria1(d, label):
    server = d.get("server") or f"{d.get('server_addr','')}:{d.get('server_port','')}"
    host, _, port = server.rpartition(":")
    auth = d.get("auth_str") or d.get("auth") or ""
    sni = d.get("server_name") or d.get("peer") or ""
    insecure = 1 if d.get("insecure") else 0
    up, down = d.get("up_mbps"), d.get("down_mbps")
    qs = _qs({"auth": auth, "peer": sni, "insecure": insecure,
              "upmbps": up, "downmbps": down, "alpn": d.get("alpn", "h3")})
    url = f"hysteria://{_hp(host, port)}?{qs}#{urllib.parse.quote(label)}"
    return _mk_node("hysteria", host, port, "auth_str", sni, insecure, _one(url, label),
                    _hp(host, port) + (f" · {up}/{down} Mbps" if up and down else ""))


def parse_hysteria2(auth, host, port, sni, insecure, up=None, down=None, label=""):
    qs = _qs({"sni": sni or "", "insecure": insecure, "upmbps": up, "downmbps": down})
    url = f"hysteria2://{urllib.parse.quote(str(auth))}@{_hp(host, port)}?{qs}#{urllib.parse.quote(label)}"
    extra = f" · {up}/{down} Mbps" if up and down else ""
    return _mk_node("hysteria2", host, port, "auth", sni, insecure, _one(url, label),
                    _hp(host, port) + extra)


def parse_clash_proxy(p, label):
    t = p.get("type")
    insecure = 1 if p.get("skip-cert-verify") else 0
    if t == "hysteria2":
        return parse_hysteria2(p.get("password"), p["server"], p["port"], p.get("sni"),
                               insecure, _digits(p.get("up")), _digits(p.get("down")), label=label)
    if t == "hysteria":
        return parse_hysteria1(dict(server=f"{p['server']}:{p['port']}",
                                    auth_str=p.get("auth-str") or p.get("auth_str") or p.get("auth") or "",
                                    server_name=p.get("sni", ""), insecure=insecure,
                                    up_mbps=_digits(p.get("up")), down_mbps=_digits(p.get("down")),
                                    alpn=",".join(p.get("alpn", ["h3"]))), label)
    if t == "vless":
        net = p.get("network", "tcp")
        reality = p.get("reality-opts") or {}
        qs = _qs({"encryption": p.get("encryption", "none"), "security": p.get("security", "tls"),
                  "sni": p.get("servername", ""), "fp": p.get("client-fingerprint", ""),
                  "pbk": reality.get("public-key", ""), "sid": reality.get("short-id", ""),
                  "type": net, "path": (p.get("http-opts") or {}).get("path", [""])[0] if net == "http" else p.get("path", "")})
        url = f"vless://{p['uuid']}@{_hp(p['server'], p['port'])}?{qs}#{urllib.parse.quote(label)}"
        return _mk_node("vless", p["server"], p["port"], "uuid", p.get("servername"), None,
                        _one(url, label), _hp(p['server'], p['port']))
    if t == "trojan":
        qs = _qs({"sni": p.get("sni", ""), "allowInsecure": insecure})
        url = (f"trojan://{urllib.parse.quote(str(p['password']))}@{_hp(p['server'], p['port'])}"
               f"?{qs}#{urllib.parse.quote(label)}")
        return _mk_node("trojan", p["server"], p["port"], "password", p.get("sni"), insecure,
                        _one(url, label), _hp(p['server'], p['port']))
    if t == "ss":
        userinfo = base64.urlsafe_b64encode(f"{p['cipher']}:{p['password']}".encode()).decode().rstrip("=")
        url = f"ss://{userinfo}@{_hp(p['server'], p['port'])}#{urllib.parse.quote(label)}"
        return _mk_node("shadowsocks", p["server"], p["port"], p["cipher"], None, None,
                        _one(url, label), f"{_hp(p['server'], p['port'])} · {p['cipher']}")
    return None


# ---------- 内容驱动的主解析入口 ----------
def parse_config_text(text: str, label: str):
    """探测配置结构 -> 解析出槽位里的全部节点（一个槽位常塞多个节点）。返回 (nodes, raw)。"""
    try:
        if text.lstrip().startswith(("{", "[")):
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except Exception:
        return [_mk_node("?", "-", "-", "-", "-", None, [], "配置格式异常，请看原始内容")], text
    if not isinstance(data, dict):
        return [_mk_node("?", "-", "-", "-", "-", None, [], "配置内容异常")], text

    nodes = []
    try:
        if "auth_str" in data and "server" in data:
            nodes = [parse_hysteria1(data, label)]
        elif "auth" in data and "bandwidth" in data and "server" in data:
            host, _, port = data["server"].rpartition(":")
            tls = data.get("tls", {})
            nodes = [parse_hysteria2(data["auth"], host, port, tls.get("sni"),
                                     1 if tls.get("insecure") else 0, label=label)]
        elif "proxies" in data and isinstance(data["proxies"], list) and data["proxies"]:
            # clash 格式：解析全部支持的代理
            for i, p in enumerate(data["proxies"]):
                nd = parse_clash_proxy(p, f"{label}#{i + 1}")
                if nd:
                    nodes.append(nd)
            if not nodes:
                p = data["proxies"][0]
                nodes = [_mk_node(f"clash/{p.get('type', '?')}", "-", "-", "-", "-", None, [],
                                  f"协议 {p.get('type')} 暂不支持转链接，请看原始内容")]
        else:
            obs = data.get("outbounds")
            if isinstance(obs, list) and obs and obs[0].get("protocol") == "vless":
                nodes = [parse_xray(data, label)]
    except Exception as e:
        return [_mk_node("?", "-", "-", "-", "-", None, [], f"解析失败：{e}")], text

    if not nodes:
        nodes = [_mk_node("?", "-", "-", "-", "-", None, [], "未识别的配置结构，请看原始内容")]
    return nodes, text


def nodes_fingerprint(nodes):
    """节点列表的指纹，用于新旧对比。auth 必须参与：同 IP 换 uuid/密码是换节点，不是没更新。"""
    return [(n["protocol"], n["server"], str(n["port"]), str(n["auth"])) for n in nodes]


# ---------- 汇总 / 去重 ----------
def _all_jobs():
    """全部工具的全部槽位：[(tool_key, slot), ...]"""
    return [(k, s) for k, t in TOOLS.items() for s in range(1, t["slots"] + 1)]


def slot_dup_tags(tool_key: str, slots: int):
    """根据缓存判断哪些槽位内容和其他槽位重复。返回 ["同源1", None, ...]（None=不重复或没拉过）。"""
    tags = [None] * slots
    seen = {}  # 指纹 -> 首次出现的槽位号
    for s in range(1, slots + 1):
        text = read_cached(tool_key, s)
        if not text:
            continue
        nodes, _ = parse_config_text(text, "")
        fp = tuple(nodes_fingerprint(nodes))
        if not fp:  # 解析失败的空指纹不参与重复判断
            continue
        if fp in seen:
            tags[s - 1] = f"同源{seen[fp]}"
        else:
            seen[fp] = s
    return tags


def warm_cache():
    """冷启动预热：cache/ 是运行时产物（clone 下来是空的），后台并发把全部槽位拉一遍写进缓存，
    这样"同源N"标记和 /sub 订阅第一次用就有数据。"""
    jobs = [j for j in _all_jobs() if not read_cached(*j)]
    if not jobs:
        return

    def work(job):
        key, s = job
        try:
            text, _ = fetch_remote(key, s)
            write_cache(key, s, text)
        except Exception:
            pass  # 预热失败不打扰启动，用户点拉取时会看到具体错误

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, jobs))


def aggregate_unique(refresh=False):
    """
    拉取全部工具的全部槽位（refresh=True 时联网刷新并更新缓存，否则读缓存），
    按指纹（协议+地址+端口+认证）去重。返回 (unique_nodes, errors)。
    """
    jobs = _all_jobs()
    parsed = {}  # (tool, slot) -> nodes or None

    if refresh:
        def work(job):
            key, s = job
            try:
                text, _ = fetch_remote(key, s)
                nodes, _ = parse_config_text(text, f"{TOOLS[key]['label']}-源{s}")
                write_cache(key, s, text)
                return nodes
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=8) as ex:
            for job, nodes in zip(jobs, ex.map(work, jobs)):
                parsed[job] = nodes
    else:
        for job in jobs:
            key, s = job
            text = read_cached(key, s)
            parsed[job] = parse_config_text(text, f"{TOOLS[key]['label']}-源{s}")[0] if text else None

    unique, seen, errors = [], set(), []
    for (key, s), nodes in parsed.items():
        if nodes is None:
            errors.append(f"{TOOLS[key]['label']} 源{s}")
            continue
        for n in nodes:
            fpk = (n["protocol"], n["server"], str(n["port"]), str(n["auth"]))
            if fpk in seen:
                continue
            seen.add(fpk)
            unique.append(dict(protocol=n["protocol"], summary=n["summary"], v2n=n["v2n"],
                               v2n_note=n["v2n_note"], clients=n["clients"], links=n["links"],
                               from_tool=TOOLS[key]["label"], from_slot=s))
    return unique, errors


def iter_unique_links(v2n_only=True):
    """全部缓存去重后的可用链接，yield (url, label)。v2rayN 订阅和 Clash 订阅共用这条去重管线。"""
    seen = set()
    for key, s in _all_jobs():
        text = read_cached(key, s)
        if not text:
            continue
        nodes, _ = parse_config_text(text, f"{TOOLS[key]['label']}-源{s}")
        for n in nodes:
            if v2n_only and not n["v2n"]:
                continue  # v2rayN 不支持的协议不进订阅，避免噪音
            fpk = (n["protocol"], n["server"], str(n["port"]), str(n["auth"]))
            if fpk in seen:
                continue
            seen.add(fpk)
            for l in n["links"]:
                yield l["url"], l["name"]
    # 外部公共源：TCP 系已测活、hy1/hy2 免检，已按 (协议,地址,端口) 去重并分别限量
    for url, name, _ in ext_alive():
        if v2n_only and url.startswith("hysteria://"):
            continue  # v2rayN 不认 hysteria v1，只让它进 Clash 订阅
        yield url, name


def build_sub():
    """v2rayN 订阅（base64 分享链接列表）。"""
    urls = [url for url, _ in iter_unique_links()]
    return base64.b64encode("\n".join(urls).encode()).decode()


_VMESS_TLS = ("tls", True, "1")  # vmess 的 tls 字段在 base64 JSON 里，各家源写法不一


def _vmess_json(url):
    """vmess:// 的 base64 JSON 体；坏链接返回 None。
    body 里可能含 "/"，所以只能按前缀切，不能交给 urlparse。"""
    body = url[8:].split("#", 1)[0]
    try:
        return json.loads(base64.b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None


def link_name(url):
    """节点显示名：优先 URL fragment，vmess 取 JSON 里的 ps 字段。"""
    u = urllib.parse.urlparse(url)
    if u.fragment:
        return urllib.parse.unquote(u.fragment)
    if url.startswith("vmess://"):
        d = _vmess_json(url) or {}
        return d.get("ps") or "节点"
    return "节点"


def _clash_proxy(url, name):
    """分享链接 -> mihomo 代理 dict。字段来源与生成链接时一一对应。"""
    u = urllib.parse.urlparse(url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    ins = {"skip-cert-verify": True}
    if u.scheme == "vmess":
        d = _vmess_json(url)
        if not d:
            return None  # 坏链接：公共源里难免混入，跳过比打挂整个订阅好
        p = dict(name=name, type="vmess", server=d.get("add"), port=int(d.get("port") or 0),
                 uuid=d.get("id"), alterId=int(d.get("aid") or 0),
                 cipher=d.get("scy") or d.get("cipher") or "auto", udp=True)
        if d.get("tls") in _VMESS_TLS:
            p["tls"] = True
            if d.get("sni"):
                p["servername"] = d["sni"]
        net = d.get("net")
        if net in ("ws", "h2", "http"):  # http 是 tcp+HTTP 伪装，别错当 h2
            p["network"] = net
            if d.get("path"):
                p[f"{net}-opts"] = {"path": d["path"]}
            if net == "ws" and d.get("host"):
                p.setdefault("ws-opts", {}).setdefault("headers", {})["Host"] = d["host"]
        return p
    if u.scheme == "vless":
        net = q.get("type", "tcp")
        p = dict(name=name, type="vless", server=u.hostname, port=u.port, uuid=u.username,
                 udp=True, network=net, tls=q.get("security") in ("tls", "reality"),
                 servername=q.get("sni", ""))
        if q.get("fp"):
            p["client-fingerprint"] = q["fp"]
        if q.get("security") == "reality":
            p["reality-opts"] = {"public-key": q["pbk"], "short-id": q.get("sid", "")}
        if net in ("ws", "http") and q.get("path"):
            p["ws-opts" if net == "ws" else "http-opts"] = {"path": q["path"]}
        return p
    if u.scheme == "trojan":
        p = dict(name=name, type="trojan", server=u.hostname, port=u.port, udp=True,
                 password=urllib.parse.unquote(u.username or ""), sni=q.get("sni", ""))
        if q.get("allowInsecure") == "1":
            p.update(ins)
        return p
    if u.scheme == "ss":
        if u.username:  # 新格式：ss://base64(cipher:pass)@host:port
            raw = u.username
            cipher, _, pw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode().partition(":")
            server, port = u.hostname, u.port
        else:  # 旧格式：整段 base64(cipher:pass@host:port)
            body = u.netloc.split("#", 1)[0]
            userinfo, _, hostport = base64.urlsafe_b64decode(
                body + "=" * (-len(body) % 4)).decode().rpartition("@")
            cipher, _, pw = userinfo.partition(":")
            server, _, port = hostport.rpartition(":")
        return dict(name=name, type="ss", server=server, port=int(port),
                    cipher=cipher, password=pw, udp=True)
    if u.scheme == "hysteria2":
        auth = urllib.parse.unquote(u.username or "")
        if u.password:
            auth += ":" + urllib.parse.unquote(u.password)
        p = dict(name=name, type="hysteria2", server=u.hostname, port=u.port, password=auth,
                 sni=q.get("sni", ""))
        if q.get("insecure") == "1":
            p.update(ins)
        if q.get("upmbps"):
            p.update(up=int(q["upmbps"]), down=int(q.get("downmbps", q["upmbps"])))
        return p
    if u.scheme == "hysteria":  # v1：v2rayN 不认但 mihomo 支持，mac 用户正好用得上
        p = dict(name=name, type="hysteria", server=u.hostname, port=u.port, protocol="udp",
                 sni=q.get("peer", ""), alpn=[q.get("alpn", "h3")])
        p["auth-str"] = q.get("auth", "")
        if q.get("insecure") == "1":
            p.update(ins)
        # 外部源的带宽写法五花八门（"55 Mbps"/"55"），只取开头数字，取不出就不写让 mihomo 用默认值
        def _mbps(v):
            m = re.match(r"\d+", str(v))
            return int(m.group()) if m else None
        up = _mbps(q.get("upmbps"))
        if up:
            down = _mbps(q.get("downmbps", up)) or up
            p.update(up=up, down=down)
        return p
    if u.scheme == "tuic":
        p = dict(name=name, type="tuic", server=u.hostname, port=u.port, uuid=u.username,
                 password=urllib.parse.unquote(u.password or ""),
                 congestion_controller=q.get("congestion_control", "bbr"))
        if q.get("alpn"):
            p["alpn"] = q["alpn"].split(",")
        if q.get("sni"):
            p["sni"] = q["sni"]
        if q.get("allow_insecure") == "1":
            p.update(ins)
        return p
    return None  # 其余协议 mihomo 不支持


def build_sub_clash():
    """Clash / Mihomo 订阅（YAML）。比 base64 订阅多收 hysteria v1。"""
    proxies, names, taken = [], [], set()
    for url, _ in iter_unique_links(v2n_only=False):
        base = link_name(url)
        name, i = base, 2
        while name in taken:
            name = f"{base} {i}"
            i += 1
        try:
            p = _clash_proxy(url, name)  # 不支持的协议在这里返回 None；字段奇葩的坏链接会抛异常
        except Exception:
            continue  # 跳过比打挂整个订阅好
        if p:
            proxies.append(p)
            names.append(name)
            taken.add(name)
    if not proxies:
        return "proxies: []\n"
    # 最小可运行配置：手动选择组 + 自动测速组（免费节点死得快，让 url-test 自己挑活的）
    return yaml.dump({
        "proxies": proxies,
        "proxy-groups": [
            {"name": "🚀 节点选择", "type": "select", "proxies": ["♻️ 自动选择"] + names},
            {"name": "♻️ 自动选择", "type": "url-test", "proxies": list(names),
             "url": "http://www.gstatic.com/generate_204", "interval": 120,
             "tolerance": 100, "lazy": True, "max-fails": 3},
        ],
        "rules": ["MATCH,🚀 节点选择"],
    }, allow_unicode=True, sort_keys=False)


# ---------- 外部公共订阅源 ----------
# 收录与面板四个槽位类型对应的协议：vmess/vless/trojan/ss（TCP 系，真握手测活）+ hysteria/hysteria2
# （QUIC 走 UDP，无法 TCP 握手测活，免检直收，挑活交给客户端 url-test 组）

def _parse_link(link):
    """分享链接 -> (scheme, host, port)，用于去重和测活。vmess 的地址在 base64 JSON 里。"""
    scheme, _, rest = link.partition("://")
    if scheme == "vmess":
        d = _vmess_json(link) or {}
        return (scheme, d.get("add"), int(d.get("port") or 0)) if d.get("add") else None
    try:
        u = urllib.parse.urlparse(link)
        return (scheme, u.hostname, u.port) if u.hostname and u.port else None
    except Exception:
        return None


def ext_cache_file(key):
    return CACHE_DIR / f"ext_{key}.json"


def _link_tls(link):
    """链接是否走 TLS 及其 SNI。vmess 看 JSON 的 tls 字段；trojan 本身就是 TLS；
    其余看 security 查询参数。"""
    scheme = link.partition("://")[0]
    if scheme == "vmess":
        d = _vmess_json(link) or {}
        return d.get("tls") in _VMESS_TLS, d.get("sni") or d.get("host") or ""
    try:
        u = urllib.parse.urlparse(link)
        q = urllib.parse.parse_qs(u.query)
        if scheme == "trojan":
            return True, (q.get("sni") or [""])[0] or (u.hostname or "")
        return (q.get("security") or [""])[0] in ("tls", "reality"), \
            (q.get("sni") or q.get("host") or [""])[0]
    except Exception:
        return False, ""


def _link_ws(link):
    """ws/httpupgrade 传输的 (HTTP Host, path)，用于 101 升级探测；非 ws 传输返回 None。"""
    scheme = link.partition("://")[0]
    if scheme == "vmess":
        d = _vmess_json(link) or {}
        return (d.get("host") or "", d.get("path") or "/") if d.get("net") in ("ws", "httpupgrade") else None
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if (q.get("type") or ["tcp"])[0] in ("ws", "httpupgrade"):
            return (q.get("host") or [""])[0], (q.get("path") or ["/"])[0]
        return None
    except Exception:
        return None


def _probe_link(link):
    """真握手测活，返回 ms 或 None。TLS 节点必须完成 SSL 握手；ws/httpupgrade 节点还要求服务器
    返回 101 升级成功——挡掉 CDN 边缘 TCP 通但节点实际已死的链接（公共源里占大头）。"""
    p = _parse_link(link)
    if not p:
        return None
    _, host, port = p
    use_tls, sni = _link_tls(link)
    ws = _link_ws(link)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    for af, st, pr, _, sa in infos:
        s = None
        try:
            s = socket.socket(af, st, pr)
            s.settimeout(4)
            t0 = time.time()
            s.connect(sa)
            if use_tls:  # 复用模块级 SSL_CTX：每节点新建要重复加载根证书，几百个节点很贵
                s = SSL_CTX.wrap_socket(s, server_hostname=sni or host)
            if ws is not None:
                hhost, path = ws
                s.sendall((f"GET {path} HTTP/1.1\r\nHost: {hhost or sni or host}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
                buf = b""
                while b"\n" not in buf and len(buf) < 256:
                    chunk = s.recv(128)
                    if not chunk:
                        break
                    buf += chunk
                if b" 101" not in buf.split(b"\n", 1)[0]:
                    raise OSError("ws 升级失败")
            return (time.time() - t0) * 1000
        except Exception:
            continue
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    return None


def refresh_ext(key):
    """拉取一个外部源 -> 解析去重 -> 分两路处理 -> 写缓存：
    TCP 系（vmess/vless/trojan/ss）真握手测活，按延迟保留；hy1/hy2 走 UDP 无法 TCP 握手测活，
    免检直收（挑活交给客户端的 url-test 组）。拉取顺序：raw -> jsDelivr -> wget.la 反代镜像。"""
    src = EXT_SOURCES[key]
    chain = [(src["url"], "github"), (src["mirror"], "jsDelivr")]
    if src.get("relay"):
        chain.append((src["relay"], "wget.la镜像"))
    text, source = None, None
    for i, (url, sname) in enumerate(chain):
        try:
            with open_url(url, timeout=45) as r:
                text = r.read().decode("utf-8", errors="replace")
            source = sname
            break
        except Exception:
            if i < len(chain) - 1:
                time.sleep(2)
    if text is None:
        raise RuntimeError(net_diagnosis())
    if "://" not in text[:4000]:  # 整体 base64 的订阅
        text = base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4)).decode("utf-8", "replace")
    total, tcp, hy1, hy2, seen = 0, [], [], [], set()
    for line in text.splitlines():
        line = line.strip()
        p = _parse_link(line) if "://" in line else None
        if not p or p[0] not in _EXT_SCHEMES or p in seen:  # 只收面板四类槽位对应的协议
            continue
        seen.add(p)
        total += 1
        if p[0] == "hysteria2":
            hy2.append(line)
        elif p[0] == "hysteria":
            hy1.append(line)
        else:
            tcp.append(line)
    # 送测前把 vmess 排后面：公共源的 vless/trojan 常堆在文件末尾，vmess 量大质次，先测更值钱的
    tcp.sort(key=lambda l: l.startswith("vmess://"))
    with ThreadPoolExecutor(max_workers=60) as ex:
        results = list(ex.map(lambda l: (l, _probe_link(l)), tcp[:EXT_PROBE_LIMIT]))
    alive = sorted((ms, url) for url, ms in results if ms is not None)
    keep_tcp = [url for _, url in alive[:EXT_KEEP_TCP]]
    ms_of = {url: round(ms) for ms, url in alive[:EXT_KEEP_TCP]}
    keep = keep_tcp + hy1[:EXT_KEEP_HY1] + hy2[:EXT_KEEP_HY2]
    data = dict(fetched=datetime.now().strftime("%Y-%m-%d %H:%M"), source=source,
                total=total, tcp=len(tcp), tcp_alive=len(keep_tcp),
                hy1=min(len(hy1), EXT_KEEP_HY1), hy2=min(len(hy2), EXT_KEEP_HY2), keep=len(keep),
                nodes=[dict(url=u, name=link_name(u), ms=ms_of.get(u)) for u in keep])
    ext_cache_file(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def ext_alive():
    """全部外部源的节点：跨源按 (协议,地址,端口) 去重；TCP 系跨源按测活延迟排序后限量，
    hy1/hy2 免检直收分别限量。TCP 系进缓存前已真握手测活；hy1/hy2 靠客户端的 url-test 组自己挑。"""
    tcp, hy1, hy2 = [], [], []  # tcp 元素带测活延迟 ms，跨源统一排序
    seen = set()
    for key in EXT_SOURCES:
        f = ext_cache_file(key)
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in d.get("nodes", []):
            p = _parse_link(n["url"])
            if not p or p[0] not in _EXT_SCHEMES or p in seen:  # 协议过滤兜底：旧版缓存只存了别的协议也不放行
                continue
            seen.add(p)
            if p[0] == "hysteria2":
                hy2.append((n["url"], n["name"], d.get("fetched", "")))
            elif p[0] == "hysteria":
                hy1.append((n["url"], n["name"], d.get("fetched", "")))
            else:
                tcp.append((n.get("ms") or 99999, n["url"], n["name"], d.get("fetched", "")))
    tcp.sort(key=lambda t: t[0])  # 各源各自排过序，这里跨源再统一排一次，截断才公平
    return ([(u, n, f) for _, u, n, f in tcp[:EXT_KEEP_TCP]]
            + hy1[:EXT_KEEP_HY1] + hy2[:EXT_KEEP_HY2])


def refresh_ext_all(background=True):
    """刷新全部外部源。默认后台跑，**四个源并行**（各自下载 + 真握手测活，总耗时约等于最慢
    的一个源，通常一分多钟），立即返回。已在刷新时直接跳过。"""
    global EXT_REFRESHING
    if EXT_REFRESHING:
        return False
    EXT_REFRESHING = True

    def refresh_one(key):
        EXT_ATTEMPT[key] = time.time()
        try:
            refresh_ext(key)
            EXT_ERRORS.pop(key, None)
        except Exception as e:
            EXT_ERRORS[key] = str(e)
            print(f"外部源 {key} 刷新失败: {e}")

    def work():
        global EXT_REFRESHING
        try:
            with ThreadPoolExecutor(max_workers=len(EXT_SOURCES)) as ex:
                list(ex.map(refresh_one, EXT_SOURCES))
        finally:
            EXT_REFRESHING = False

    if background:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()
    return True


# ---------- 本地状态 ----------
def local_summary(tool_key: str):
    """工具列表里显示的本地概要：该工具所有槽位缓存里最新的一份。"""
    t = TOOLS[tool_key]
    best = max(CACHE_DIR.glob(f"{tool_key}_ip*"), key=lambda f: f.stat().st_mtime, default=None)
    if best:
        slot = int(re.search(r"_ip(\d+)", best.name).group(1))
        nodes, _ = parse_config_text(best.read_text(encoding="utf-8", errors="replace"), f"{t['label']}-ip{slot}")
        return dict(node=nodes[0], count=len(nodes),
                    mtime=datetime.fromtimestamp(best.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    source=f"上次拉的（源{slot}）")
    return None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}]", fmt % args)

    def _send(self, body: bytes, ctype: str, code=200, **headers):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            # 禁缓存：页面迭代频繁，浏览器缓存旧 JS 会导致新功能"没生效"
            self._send((PANEL_DIR / "index.html").read_bytes(), "text/html; charset=utf-8",
                       Cache_Control="no-store, must-revalidate")
        elif self.path == "/api/tools":
            tools = []
            for key, t in TOOLS.items():
                loc = local_summary(key)
                if loc and loc.get("node") and loc["node"].get("protocol") not in ("?", None):
                    # 徽章跟随本地实际配置的协议（槽位内容会漂移，比如 clash.meta 槽位现在是 hysteria v1）
                    badge = ("v2rayN 可导入", "ok") if loc["node"].get("v2n") else ("v2rayN 不支持", "no")
                else:
                    badge = TOOL_BADGE[key]  # 没有本地记录时按工具的典型协议标注
                tools.append(dict(key=key, label=t["label"], sub=t["sub"], slots=t["slots"],
                                  badge=badge, local=loc,
                                  slot_tags=slot_dup_tags(key, t["slots"])))
            self._json(dict(panel_dir=str(PANEL_DIR), tools=tools, warming=WARMING,
                            sub_url=f"http://127.0.0.1:{PORT}/sub"))
        elif self.path == "/api/ext":
            exts = []
            for key, src in EXT_SOURCES.items():
                info = dict(key=key, label=src["label"])
                f = ext_cache_file(key)
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    info.update(fetched=d.get("fetched"), total=d.get("total"),
                                tcp=d.get("tcp", 0), tcp_alive=d.get("tcp_alive", 0),
                                hy1=d.get("hy1", 0), hy2=d.get("hy2", 0),
                                keep=len(d.get("nodes", [])))
                except Exception:
                    info.update(fetched=None, total=None, tcp=0, tcp_alive=0, hy1=0, hy2=0, keep=0)
                info["failed_at"] = (datetime.fromtimestamp(EXT_ATTEMPT[key]).strftime("%H:%M")
                                     if key in EXT_ATTEMPT else None)  # 失败原因属于哪一轮，避免旧失败记录误导
                info["error"] = EXT_ERRORS.get(key)
                exts.append(info)
            proxy = _PROXY["url"]  # 只回缓存值，不在请求线程里现探
            self._json(dict(ext=exts, refreshing=EXT_REFRESHING,
                            keep_tcp=EXT_KEEP_TCP, keep_hy1=EXT_KEEP_HY1, keep_hy2=EXT_KEEP_HY2,
                            proxy=proxy, proxy_checked=bool(_PROXY["ts"]),
                            proxy_cfg=_user_proxy))
        elif self.path == "/sub":
            ua = self.headers.get("User-Agent", "").lower()
            if "clash" in ua or "mihomo" in ua:
                # Clash Verge 等只认 YAML 格式订阅，v2rayN 认 base64 链接列表，按 UA 分流
                self._send(build_sub_clash().encode("utf-8"), "text/yaml; charset=utf-8")
            else:
                self._send(build_sub().encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self._json(dict(error="not found"), 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json(dict(error="请求体不是合法 JSON"), 400)

        if self.path == "/api/ext_refresh":
            if EXT_REFRESHING:  # 连点两下别起两个刷新线程
                return self._json(dict(ok=True, hint="已在刷新中"))
            refresh_ext_all(background=True)
            return self._json(dict(ok=True, hint="后台刷新中，TCP 节点要真握手测活，通常一分多钟"))

        if self.path == "/api/proxy":
            global _user_proxy
            norm, err = normalize_proxy(body.get("proxy", ""))
            if err:
                return self._json(dict(ok=False, error=err))
            _user_proxy = norm
            try:
                PROXY_CFG_FILE.write_text(json.dumps({"proxy": norm}, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                return self._json(dict(ok=False, error=f"保存失败：{e}"))
            with _PROXY_LOCK:  # 配置变了立刻作废探测缓存，后台重探一次
                _PROXY.update(url=None, ts=0.0)
            threading.Thread(target=detect_proxy, args=(True,), daemon=True).start()
            hint = ("已保存，面板会优先走这个代理（连不通会自动落到直连 + 镜像）" if norm
                    else "已清除。以后不自动试代理，直接走直连 + 镜像")
            return self._json(dict(ok=True, hint=hint))

        if self.path == "/api/fetch_all":
            try:
                unique, errors = aggregate_unique(refresh=True)
                return self._json(dict(unique=unique, errors=errors))
            except Exception as e:
                return self._json(dict(error=str(e)), 502)

        if self.path != "/api/fetch":
            return self._json(dict(error="not found"), 404)
        tool = body.get("tool")
        try:
            slot = int(body.get("slot", 1))
        except (TypeError, ValueError):
            return self._json(dict(error="源编号必须是数字"), 400)
        if tool not in TOOLS:
            return self._json(dict(error=f"未知工具 {tool}"), 400)
        if not (1 <= slot <= TOOLS[tool]["slots"]):
            return self._json(dict(error=f"没有这个源，{TOOLS[tool]['label']} 只有源1~源{TOOLS[tool]['slots']}"), 400)
        try:
            text, source = fetch_remote(tool, slot)
            nodes, _ = parse_config_text(text, f"{TOOLS[tool]['label']}-ip{slot}")
            old_text = read_cached(tool, slot)
            old_nodes = parse_config_text(old_text, f"{TOOLS[tool]['label']}-旧")[0] if old_text else []
            changed = nodes_fingerprint(old_nodes) != nodes_fingerprint(nodes) if old_nodes else None
            write_cache(tool, slot, text)  # 每次拉取都更新缓存（冷启动后下一次就有"上次拉取"了）
            return self._json(dict(source=source, nodes=nodes, old_nodes=old_nodes,
                                   changed=changed, raw=text, first_time=not old_nodes))
        except Exception as e:
            return self._json(dict(error=str(e)), 502)


def main():
    global WARMING
    print(f"ChromeGo 节点面板  ->  http://127.0.0.1:{PORT}")
    print(f"面板目录          ->  {PANEL_DIR}")
    print(f"配置缓存          ->  {CACHE_DIR}")
    # 后台预热：把缺缓存的槽位拉一遍，"同源N"标记和 /sub 第一次用就有数据
    def _warm():
        global WARMING
        warm_cache()
        WARMING = False
        print("预热完成：全部槽位缓存就绪")
    WARMING = True
    threading.Thread(target=_warm, daemon=True).start()
    # 外部源完全靠用户手点「立即更新」，不做任何自动拉取；启动时先探一次代理，
    # 不然没有刷新请求来触发探测，面板会一直显示"正在看网络情况"
    threading.Thread(target=detect_proxy, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
