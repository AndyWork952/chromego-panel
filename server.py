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
import ssl
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

import yaml

WARMING = False  # 冷启动预热中标志（预热线程完成后置 False）

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
SUPPORT = {
    "vless":       {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
    "vmess":       {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
    "trojan":      {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
    "shadowsocks": {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
    "hysteria2":   {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
    "tuic":        {"v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"},
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


def build_sub():
    """把缓存里全部去重后的可用链接聚合成 v2rayN 订阅（base64）。"""
    lines, seen = [], set()
    for key, s in _all_jobs():
        text = read_cached(key, s)
        if not text:
            continue
        nodes, _ = parse_config_text(text, f"{TOOLS[key]['label']}-源{s}")
        for n in nodes:
            if not n["v2n"]:
                continue  # v2rayN 不支持的协议不进订阅，避免噪音
            fpk = (n["protocol"], n["server"], str(n["port"]), str(n["auth"]))
            if fpk in seen:
                continue
            seen.add(fpk)
            for l in n["links"]:
                lines.append(l["url"])
    return base64.b64encode("\n".join(lines).encode()).decode()


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
        elif self.path == "/sub":
            self._send(build_sub().encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self._json(dict(error="not found"), 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json(dict(error="请求体不是合法 JSON"), 400)

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
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
