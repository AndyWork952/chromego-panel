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

# 外部公共订阅源（2026-10-26 实测存活率最高的两个）：拉取 -> 解析链接 -> TCP 测活 -> 只留活节点
# mirror 是 jsDelivr CDN 兜底——raw.githubusercontent 在国内网络经常被重置
EXT_SOURCES = {
    "aliilapro": dict(label="ALIILAPRO",
                      url="https://raw.githubusercontent.com/ALIILAPRO/v2rayNG-Config/main/sub.txt",
                      mirror="https://cdn.jsdelivr.net/gh/ALIILAPRO/v2rayNG-Config@main/sub.txt"),
    "pawdroid":  dict(label="Pawdroid",
                      url="https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub",
                      mirror="https://cdn.jsdelivr.net/gh/Pawdroid/Free-servers@main/sub"),
}
EXT_PROBE_LIMIT = 600  # 每源最多探测的节点数（控制刷新耗时在 1 分钟级）
EXT_KEEP = 300         # 每源最多保留进订阅的活节点数（控制订阅体积）
# 免费源的 vmess 节点存活期通常只有半小时级，vless/trojan 明显耐用；默认过滤掉 vmess，可在面板上关掉
_ext_cfg = dict(drop_vmess=True)

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

TOOLS = {
    "xray":       dict(label="Xray",        sub="VLESS / REALITY",      config="config.json", remote="xray",       slots=4),
    "clash.meta": dict(label="Clash.Meta",  sub="Hysteria / Hysteria2", config="config.yaml", remote="clash.meta2", slots=6),
    "hysteria":   dict(label="Hysteria v1", sub="QUIC 混淆",           config="config.json", remote="hysteria",   slots=4),
    "hysteria2":  dict(label="Hysteria2",   sub="QUIC",                 config="config.json", remote="hysteria2",  slots=4),
    "juicity":    dict(label="Juicity",     sub="QUIC + BBR",          config="config.json", remote="juicity",    slots=2),
    "naiveproxy": dict(label="NaiveProxy",  sub="HTTPS2 伪装",         config="config.json", remote="naiveproxy", slots=2),
    "singbox":    dict(label="Sing-box",    sub="TUIC / Hysteria",      config="config.json", remote="singbox",    slots=2),
}

# 没有本地记录时按工具典型协议给的兜底徽章（有记录后以实际解析出的协议为准）
TOOL_BADGE = {
    "xray":      ("v2rayN 可导入", "ok"),
    "hysteria2": ("v2rayN 可导入", "ok"),
    "clash.meta": ("v2rayN 可导入", "ok"),
    "singbox":   ("看槽位内容", "mid"),   # 槽位可能是 TUIC(可导入) 或 hysteria v1(不可)
    "hysteria":  ("v2rayN 不支持", "no"),
    "naiveproxy": ("v2rayN 不支持", "no"),
    "juicity":   ("v2rayN 不支持", "no"),
}


# ---------- 拉取 ----------
def fetch_remote(tool_key: str, slot: int):
    """按 bat 的容错顺序：gitlab 主源 -> 67867867.xyz 镜像。返回 (text, source)。"""
    t = TOOLS[tool_key]
    errors = []
    for source, tpl in (("gitlab", GITLAB_TPL), ("mirror", MIRROR_TPL)):
        url = tpl.format(remote=t["remote"], slot=slot, filename=t["config"])
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ChromeGoPanel"})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                if r.status != 200:
                    raise OSError(f"HTTP {r.status}")
                text = r.read().decode("utf-8", errors="replace")
                if not text.strip():
                    raise OSError("空响应")
                return text, source
        except Exception as e:
            errors.append(f"{source}: {e}")
    raise RuntimeError("两个源都拉取失败：" + " | ".join(errors))


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
    """vless+reality+xhttp -> vless, naive+https -> naive 之类，取主协议名"""
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
    "tuic":        _ALL_OK,
    # hysteria v1：sing-box 和 v2rayN 都删了，但 mihomo 和 NekoRay 还活着
    "hysteria":    {"NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）", "ChromeGo 自带 hysteria.exe"},
    "naive":       {"NekoRay / NekoBox", "ChromeGo 自带 naive.exe（官方核心）"},
    "juicity":     {"NekoRay / NekoBox", "ChromeGo 自带 juicity-client.exe（官方核心）"},
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
    if p.startswith(("vless", "vmess", "trojan", "shadowsocks")) or p in ("hysteria2", "tuic"):
        return True, ""
    if p.startswith("naive"):
        return False, "v2rayN 已移除 NaiveProxy 支持"
    if p == "hysteria":
        return False, "hysteria v1 已被 v2rayN 移除（仅 hysteria2 支持）"
    if p == "juicity":
        return False, "v2rayN 不支持 juicity（sing-box 也不认）"
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


def parse_tuic(o, label):
    tls = o.get("tls", {})
    qs = _qs({"congestion_control": o.get("congestion_control", "bbr"),
              "alpn": ",".join(tls.get("alpn", ["h3"])),
              "sni": tls.get("server_name", ""),
              "allow_insecure": 1 if tls.get("insecure") else 0})
    url = (f"tuic://{o['uuid']}:{urllib.parse.quote(o['password'])}"
           f"@{_hp(o['server'], o['server_port'])}?{qs}#{urllib.parse.quote(label)}")
    return _mk_node("tuic", o["server"], o["server_port"], "uuid+password",
                    tls.get("server_name"), 1 if tls.get("insecure") else 0, _one(url, label),
                    _hp(o['server'], o['server_port']))


def parse_juicity(d, label):
    host, _, port = d["server"].rpartition(":")
    qs = _qs({"sni": d.get("sni", ""),
              "allow_insecure": 1 if d.get("allow_insecure") else 0,
              "congestion_control": d.get("congestion_control", "bbr")})
    url = (f"juicity://{d['uuid']}:{urllib.parse.quote(d['password'])}"
           f"@{_hp(host, port)}?{qs}#{urllib.parse.quote(label)}")
    return _mk_node("juicity", host, port, "uuid+password", d.get("sni"),
                    1 if d.get("allow_insecure") else 0, _one(url, label), d["server"])


def parse_naive(d, label):
    scheme, _, rest = d["proxy"].partition("://")
    url = f"naive+{scheme}://{rest}#{urllib.parse.quote(label)}"
    userinfo, _, hostport = rest.rpartition("@")
    return _mk_node("naive+" + scheme, hostport.rpartition(":")[0], hostport.rpartition(":")[-1],
                    "user:pass", None, None, _one(url, label), hostport)


def parse_singbox_outbound(o, label):
    t = o.get("type")
    if t == "tuic":
        return parse_tuic(o, label)
    if t == "hysteria2":
        tls = o.get("tls", {})
        return parse_hysteria2(o.get("password"), o["server"], o["server_port"],
                               tls.get("server_name"), 1 if tls.get("insecure") else 0,
                               o.get("up_mbps"), o.get("down_mbps"), label=label)
    if t == "hysteria":
        tls = o.get("tls", {})
        d = dict(server=f"{o['server']}:{o['server_port']}",
                 auth_str=o.get("auth_str") or o.get("auth") or "",
                 server_name=tls.get("server_name", ""),
                 insecure=1 if tls.get("insecure") else 0,
                 up_mbps=o.get("up_mbps"), down_mbps=o.get("down_mbps"), alpn=",".join(tls.get("alpn", ["h3"])))
        return parse_hysteria1(d, label)
    if t == "vless":
        tls = o.get("tls", {})
        qs = _qs({"security": "reality" if tls.get("reality") else ("tls" if tls.get("enabled") else "none"),
                  "sni": tls.get("server_name", ""), "fp": tls.get("utls", {}).get("fingerprint", ""),
                  "pbk": (tls.get("reality") or {}).get("public_key", ""),
                  "sid": (tls.get("reality") or {}).get("short_id", ""),
                  "type": o.get("transport", {}).get("type", "tcp")})
        url = f"vless://{o['uuid']}@{_hp(o['server'], o['server_port'])}?{qs}#{urllib.parse.quote(label)}"
        return _mk_node("vless", o["server"], o["server_port"], "uuid", tls.get("server_name"),
                        1 if tls.get("insecure") else 0, _one(url, label),
                        _hp(o['server'], o['server_port']))
    if t == "trojan":
        tls = o.get("tls", {})
        qs = _qs({"sni": tls.get("server_name", ""),
                  "allowInsecure": 1 if tls.get("insecure") else 0})
        url = (f"trojan://{urllib.parse.quote(o['password'])}@{_hp(o['server'], o['server_port'])}"
               f"?{qs}#{urllib.parse.quote(label)}")
        return _mk_node("trojan", o["server"], o["server_port"], "password", tls.get("server_name"),
                        1 if tls.get("insecure") else 0, _one(url, label),
                        _hp(o['server'], o['server_port']))
    if t == "shadowsocks":
        userinfo = base64.urlsafe_b64encode(f"{o['cipher']}:{o['password']}".encode()).decode().rstrip("=")
        url = f"ss://{userinfo}@{_hp(o['server'], o['server_port'])}#{urllib.parse.quote(label)}"
        return _mk_node("shadowsocks", o["server"], o["server_port"], o["cipher"], None, None,
                        _one(url, label), f"{_hp(o['server'], o['server_port'])} · {o['cipher']}")
    return None


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
SKIP_TYPES = {"direct", "block", "dns", "urltest", "selector", "socks", "http"}


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
        if "proxy" in data and "listen" in data:
            nodes = [parse_naive(data, label)]
        elif "auth_str" in data and "server" in data:
            nodes = [parse_hysteria1(data, label)]
        elif "auth" in data and "bandwidth" in data and "server" in data:
            host, _, port = data["server"].rpartition(":")
            tls = data.get("tls", {})
            nodes = [parse_hysteria2(data["auth"], host, port, tls.get("sni"),
                                     1 if tls.get("insecure") else 0, label=label)]
        elif "uuid" in data and "password" in data and "server" in data:
            nodes = [parse_juicity(data, label)]
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
            if isinstance(obs, list) and obs:
                if obs[0].get("protocol") == "vless":
                    nodes = [parse_xray(data, label)]
                else:
                    # sing-box 格式：解析全部非占位 outbound
                    for i, o in enumerate(x for x in obs if x.get("type") not in SKIP_TYPES):
                        nd = parse_singbox_outbound(o, f"{label}#{i + 1}")
                        if nd:
                            nodes.append(nd)
                    if not nodes:
                        o = next((x for x in obs if x.get("type") not in SKIP_TYPES), None)
                        if o:
                            nodes = [_mk_node(f"singbox/{o.get('type')}", "-", "-", "-", "-", None, [],
                                              f"协议 {o.get('type')} 暂不支持转链接，请看原始内容")]
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
    # 外部公共源：进来前已经过真握手测活，且内部已按 (协议,地址,端口) 去重，直接追加
    for url, name, _, _ in ext_alive():
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
        if q.get("upmbps"):
            p.update(up=int(q["upmbps"]), down=int(q.get("downmbps", q["upmbps"])))
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
    return None  # naive / juicity：mihomo 不支持


def build_sub_clash():
    """Clash / Mihomo 订阅（YAML）。比 base64 订阅多收 hysteria v1，少 naive/juicity。"""
    proxies, names, taken = [], [], set()
    for url, _ in iter_unique_links(v2n_only=False):
        base = link_name(url)
        name, i = base, 2
        while name in taken:
            name = f"{base} {i}"
            i += 1
        p = _clash_proxy(url, name)  # 不支持的协议（naive/juicity）在这里返回 None
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
_EXT_TCP_SCHEMES = ("vmess", "vless", "trojan", "ss")  # UDP 协议（hysteria 系）无法握手测活，外部源只收这四种

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


def _link_tls(link):
    """链接是否走 TLS 及其 SNI。vmess 看 JSON 的 tls 字段，其余看 security 查询参数。"""
    scheme, _, rest = link.partition("://")
    if scheme == "vmess":
        d = _vmess_json(link) or {}
        return d.get("tls") in _VMESS_TLS, d.get("sni") or d.get("host") or ""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        return (q.get("security") or [""])[0] in ("tls", "reality"), \
            (q.get("sni") or q.get("host") or [""])[0]
    except Exception:
        return False, ""


def _link_ws(link):
    """ws/httpupgrade 传输的 (HTTP Host, path)，用于 101 升级探测；非 ws 传输返回 None。"""
    scheme, _, _ = link.partition("://")
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
            if use_tls:  # 复用模块级 context：每节点新建要重复加载根证书，几百个节点很贵
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


def ext_cache_file(key):
    return CACHE_DIR / f"ext_{key}.json"


def refresh_ext(key):
    """拉取一个外部源 -> 解析去重 -> 并行真握手测活 -> 按延迟保留前 EXT_KEEP 个活节点写缓存。"""
    src = EXT_SOURCES[key]
    text, source = None, None
    for i, url in enumerate((src["url"], src["mirror"], src["url"])):  # raw -> jsDelivr -> raw 重试
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ChromeGoPanel"})
            with urllib.request.urlopen(req, timeout=45) as r:
                text = r.read().decode("utf-8", errors="replace")
            source = "jsDelivr" if i == 1 else "github"
            break
        except Exception:
            if i < 2:
                time.sleep(2)
    if text is None:
        raise RuntimeError("主源和镜像都拉取失败")
    if "://" not in text[:4000]:  # 整体 base64 的订阅
        text = base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4)).decode("utf-8", "replace")
    links, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        p = _parse_link(line) if "://" in line else None
        if p and p[0] in _EXT_TCP_SCHEMES and p not in seen:
            seen.add(p)
            links.append(line)
    # 按序截断前先把非 vmess 排前面：ALIILAPRO 的 vless 全堆在文件末尾，不排就一条都测不到
    links.sort(key=lambda l: l.startswith("vmess://"))
    pairs = links[:EXT_PROBE_LIMIT]
    with ThreadPoolExecutor(max_workers=60) as ex:
        results = list(ex.map(lambda l: (l, _probe_link(l)), pairs))
    alive = sorted((ms, url) for url, ms in results if ms is not None)
    data = dict(fetched=datetime.now().strftime("%Y-%m-%d %H:%M"), source=source,
                total=len(links), alive=len(alive),
                nodes=[dict(url=url, name=link_name(url), ms=round(ms)) for ms, url in alive[:EXT_KEEP]])
    ext_cache_file(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def ext_alive():
    """全部外部源的存活节点，跨源按 (协议,地址,端口) 再去重一次。"""
    out, seen = [], set()
    for key in EXT_SOURCES:
        f = ext_cache_file(key)
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in d.get("nodes", []):
            if _ext_cfg["drop_vmess"] and n["url"].startswith("vmess://"):
                continue
            p = _parse_link(n["url"])
            if p and p not in seen:
                seen.add(p)
                out.append((n["url"], n["name"], n["ms"], d.get("fetched", "")))
    return out


def refresh_ext_all(background=True):
    """刷新全部外部源。默认后台跑（全量测活要 1-2 分钟），立即返回。"""
    global EXT_REFRESHING
    def work():
        global EXT_REFRESHING
        EXT_REFRESHING = True
        try:
            for key in EXT_SOURCES:
                try:
                    refresh_ext(key)
                except Exception as e:
                    print(f"外部源 {key} 刷新失败: {e}")
        finally:
            EXT_REFRESHING = False
    if background:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()


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
                    source=f"上次拉取(ip{slot})")
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
                    nodes = d.get("nodes", [])
                    keep = sum(1 for n in nodes
                               if not (_ext_cfg["drop_vmess"] and n["url"].startswith("vmess://")))
                    info.update(fetched=d.get("fetched"), total=d.get("total"),
                                alive=d.get("alive"), keep=keep)
                except Exception:
                    info.update(fetched=None, total=None, alive=None, keep=0)
                exts.append(info)
            self._json(dict(ext=exts, refreshing=EXT_REFRESHING, drop_vmess=_ext_cfg["drop_vmess"]))
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
            if EXT_REFRESHING:  # 连点两下别起两个线程同时测活
                return self._json(dict(ok=True, hint="已在刷新中"))
            refresh_ext_all(background=True)
            return self._json(dict(ok=True, hint="后台刷新中，约 1-2 分钟后完成"))

        if self.path == "/api/ext_filter":
            _ext_cfg["drop_vmess"] = bool(body.get("drop_vmess"))
            return self._json(dict(ok=True, drop_vmess=_ext_cfg["drop_vmess"]))

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
            return self._json(dict(error="slot 必须是数字"), 400)
        if tool not in TOOLS:
            return self._json(dict(error=f"未知工具 {tool}"), 400)
        if not (1 <= slot <= TOOLS[tool]["slots"]):
            return self._json(dict(error=f"槽位超出范围，该工具只有 ip1~ip{TOOLS[tool]['slots']}"), 400)
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
    # 外部源：本地没有缓存时（首次启动）自动拉一次；之后靠面板上的"刷新外部源"手动更新
    if not all(ext_cache_file(k).exists() for k in EXT_SOURCES):
        threading.Thread(target=lambda: refresh_ext_all(background=False), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
