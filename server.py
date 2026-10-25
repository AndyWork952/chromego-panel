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
import re
import ssl
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

import yaml

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
    url_tpl = {"gitlab": GITLAB_TPL, "mirror": MIRROR_TPL}
    errors = []
    for source in ("gitlab", "mirror"):
        url = url_tpl[source].format(remote=t["remote"], slot=slot, filename=t["config"])
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
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def write_cache(tool_key: str, slot: int, text: str):
    cache_file(tool_key, slot).write_text(text, encoding="utf-8")


# ---------- 链接生成小工具 ----------
def _wrap_host(host: str) -> str:
    h = str(host).strip("[]")
    return f"[{h}]" if ":" in h else h


def _proto_key(protocol):
    """vless+reality+xhttp -> vless, naive+https -> naive 之类，取主协议名"""
    return str(protocol).split("+")[0].split(".")[0]


# 各协议能用的客户端（截至 v2rayN 7.x 时代的生态）
USABLE_CLIENTS = {
    "vless":       ["v2rayN", "NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）"],
    "vmess":       ["v2rayN", "NekoRay / NekoBox", "mihomo 系客户端"],
    "trojan":      ["v2rayN", "NekoRay / NekoBox", "mihomo 系客户端"],
    "shadowsocks": ["v2rayN", "NekoRay / NekoBox", "mihomo 系客户端"],
    "hysteria2":   ["v2rayN", "NekoRay / NekoBox", "mihomo 系客户端"],
    "tuic":        ["v2rayN", "NekoRay / NekoBox", "mihomo 系客户端"],
    # hysteria v1：sing-box 和 v2rayN 都删了，但 mihomo 和 NekoRay 还活着
    "hysteria":    ["NekoRay / NekoBox", "mihomo 系（Clash Verge Rev、FlClash）", "ChromeGo 自带 hysteria.exe"],
    "naive":       ["NekoRay / NekoBox", "ChromeGo 自带 naive.exe（官方核心）"],
    "juicity":     ["NekoRay / NekoBox", "ChromeGo 自带 juicity-client.exe（官方核心）"],
}


def _v2n(protocol):
    """该协议生成的分享链接能否直接导入 v2rayN（7.x，Xray + sing-box 核心）"""
    p = str(protocol)
    if p.startswith("vless") or p.startswith("vmess") or p.startswith("trojan") \
            or p.startswith("shadowsocks") or p == "hysteria2" or p == "tuic":
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
    clients = USABLE_CLIENTS.get(_proto_key(protocol), [])
    return dict(protocol=protocol, server=server, port=port, auth=auth,
                sni=sni, insecure=insecure, links=links, summary=summary,
                v2n=v2n, v2n_note=note, clients=clients)


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
    qs = urllib.parse.urlencode({k: x for k, x in params.items() if x != ""})
    url = f"vless://{u['id']}@{_wrap_host(v['address'])}:{v['port']}?{qs}#{urllib.parse.quote(label)}"
    return _mk_node(f"vless+{ss.get('security','tls')}+{ss.get('network','tcp')}",
                    v["address"], v["port"], "UUID " + u["id"][:8] + "…", params["sni"], None,
                    _one(url, label), f"{_wrap_host(v['address'])}:{v['port']} · SNI {params['sni'] or '—'}")


def parse_hysteria1(d, label):
    server = d.get("server") or f"{d.get('server_addr','')}:{d.get('server_port','')}"
    host, _, port = server.rpartition(":")
    auth = d.get("auth_str") or d.get("auth") or ""
    sni = d.get("server_name") or d.get("peer") or ""
    insecure = 1 if d.get("insecure") else 0
    up, down = d.get("up_mbps"), d.get("down_mbps")
    qs = urllib.parse.urlencode({k: x for k, x in {
        "auth": auth, "peer": sni, "insecure": insecure,
        "upmbps": up, "downmbps": down, "alpn": d.get("alpn", "h3")}.items() if x not in ("", None, 0)})
    url = f"hysteria://{_wrap_host(host)}:{port}?{qs}#{urllib.parse.quote(label)}"
    return _mk_node("hysteria", host, port, "auth_str", sni, insecure, _one(url, label),
                    f"{_wrap_host(host)}:{port}" + (f" · {up}/{down} Mbps" if up and down else ""))


def parse_hysteria2(auth, host, port, sni, insecure, up=None, down=None, label=""):
    qs = urllib.parse.urlencode({k: x for k, x in {
        "sni": sni or "", "insecure": insecure,
        "upmbps": up, "downmbps": down}.items() if x not in ("", None, 0)})
    url = f"hysteria2://{urllib.parse.quote(str(auth))}@{_wrap_host(host)}:{port}?{qs}#{urllib.parse.quote(label)}"
    extra = f" · {up}/{down} Mbps" if up and down else ""
    return _mk_node("hysteria2", host, port, "auth", sni, insecure, _one(url, label),
                    f"{_wrap_host(host)}:{port}{extra}")


def parse_tuic(o, label):
    tls = o.get("tls", {})
    qs = urllib.parse.urlencode({k: x for k, x in {
        "congestion_control": o.get("congestion_control", "bbr"),
        "alpn": ",".join(tls.get("alpn", ["h3"])),
        "sni": tls.get("server_name", ""),
        "allow_insecure": 1 if tls.get("insecure") else 0}.items() if x not in ("", None, 0)})
    url = (f"tuic://{o['uuid']}:{urllib.parse.quote(o['password'])}"
           f"@{_wrap_host(o['server'])}:{o['server_port']}?{qs}#{urllib.parse.quote(label)}")
    return _mk_node("tuic", o["server"], o["server_port"], "uuid+password",
                    tls.get("server_name"), 1 if tls.get("insecure") else 0, _one(url, label),
                    f"{_wrap_host(o['server'])}:{o['server_port']}")


def parse_juicity(d, label):
    host, _, port = d["server"].rpartition(":")
    qs = urllib.parse.urlencode({k: x for k, x in {
        "sni": d.get("sni", ""),
        "allow_insecure": 1 if d.get("allow_insecure") else 0,
        "congestion_control": d.get("congestion_control", "bbr")}.items() if x not in ("", None, 0)})
    url = (f"juicity://{d['uuid']}:{urllib.parse.quote(d['password'])}"
           f"@{_wrap_host(host)}:{port}?{qs}#{urllib.parse.quote(label)}")
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
        qs = urllib.parse.urlencode({k: x for k, x in {
            "security": "reality" if tls.get("reality") else ("tls" if tls.get("enabled") else "none"),
            "sni": tls.get("server_name", ""), "fp": tls.get("utls", {}).get("fingerprint", ""),
            "pbk": (tls.get("reality") or {}).get("public_key", ""),
            "sid": (tls.get("reality") or {}).get("short_id", ""),
            "type": o.get("transport", {}).get("type", "tcp")}.items() if x not in ("", None)})
        url = (f"vless://{o['uuid']}@{_wrap_host(o['server'])}:{o['server_port']}"
               f"?{qs}#{urllib.parse.quote(label)}")
        return _mk_node("vless", o["server"], o["server_port"], "uuid", tls.get("server_name"),
                        1 if tls.get("insecure") else 0, _one(url, label),
                        f"{_wrap_host(o['server'])}:{o['server_port']}")
    if t == "trojan":
        tls = o.get("tls", {})
        qs = urllib.parse.urlencode({k: x for k, x in {
            "sni": tls.get("server_name", ""),
            "allowInsecure": 1 if tls.get("insecure") else 0}.items() if x not in ("", None, 0)})
        url = (f"trojan://{urllib.parse.quote(o['password'])}@{_wrap_host(o['server'])}:{o['server_port']}"
               f"?{qs}#{urllib.parse.quote(label)}")
        return _mk_node("trojan", o["server"], o["server_port"], "password", tls.get("server_name"),
                        1 if tls.get("insecure") else 0, _one(url, label),
                        f"{_wrap_host(o['server'])}:{o['server_port']}")
    if t == "shadowsocks":
        userinfo = base64.urlsafe_b64encode(f"{o['cipher']}:{o['password']}".encode()).decode().rstrip("=")
        url = f"ss://{userinfo}@{_wrap_host(o['server'])}:{o['server_port']}#{urllib.parse.quote(label)}"
        return _mk_node("shadowsocks", o["server"], o["server_port"], o["cipher"], None, None,
                        _one(url, label), f"{_wrap_host(o['server'])}:{o['server_port']} · {o['cipher']}")
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
        qs = urllib.parse.urlencode({k: x for k, x in {
            "encryption": p.get("encryption", "none"), "security": p.get("security", "tls"),
            "sni": p.get("servername", ""), "fp": p.get("client-fingerprint", ""),
            "pbk": reality.get("public-key", ""), "sid": reality.get("short-id", ""),
            "type": net, "path": (p.get("http-opts") or {}).get("path", [""])[0] if net == "http" else p.get("path", ""),
        }.items() if x not in ("", None)})
        url = f"vless://{p['uuid']}@{_wrap_host(p['server'])}:{p['port']}?{qs}#{urllib.parse.quote(label)}"
        return _mk_node("vless", p["server"], p["port"], "uuid", p.get("servername"), None,
                        _one(url, label), f"{_wrap_host(p['server'])}:{p['port']}")
    if t == "trojan":
        qs = urllib.parse.urlencode({k: x for k, x in {
            "sni": p.get("sni", ""), "allowInsecure": insecure}.items() if x not in ("", None, 0)})
        url = (f"trojan://{urllib.parse.quote(str(p['password']))}@{_wrap_host(p['server'])}:{p['port']}"
               f"?{qs}#{urllib.parse.quote(label)}")
        return _mk_node("trojan", p["server"], p["port"], "password", p.get("sni"), insecure,
                        _one(url, label), f"{_wrap_host(p['server'])}:{p['port']}")
    if t == "ss":
        userinfo = base64.urlsafe_b64encode(f"{p['cipher']}:{p['password']}".encode()).decode().rstrip("=")
        url = f"ss://{userinfo}@{_wrap_host(p['server'])}:{p['port']}#{urllib.parse.quote(label)}"
        return _mk_node("shadowsocks", p["server"], p["port"], p["cipher"], None, None,
                        _one(url, label), f"{_wrap_host(p['server'])}:{p['port']} · {p['cipher']}")
    return None


# ---------- 内容驱动的主解析入口 ----------
SKIP_TYPES = {"direct", "block", "dns", "urltest", "selector", "socks", "http"}


def parse_config_text(text: str, label: str):
    """探测配置结构 -> 按实际协议解析。返回 (node, raw)。"""
    try:
        if text.lstrip().startswith(("{", "[")):
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except Exception:
        return _mk_node("?", "-", "-", "-", "-", None, [], "配置格式异常，请看原始内容"), text
    if not isinstance(data, dict):
        return _mk_node("?", "-", "-", "-", "-", None, [], "配置内容异常"), text

    try:
        if "proxy" in data and "listen" in data:
            return parse_naive(data, label), text
        if "auth_str" in data and "server" in data:
            return parse_hysteria1(data, label), text
        if "auth" in data and "bandwidth" in data and "server" in data:
            host, _, port = data["server"].rpartition(":")
            tls = data.get("tls", {})
            return parse_hysteria2(data["auth"], host, port, tls.get("sni"),
                                   1 if tls.get("insecure") else 0, label=label), text
        if "uuid" in data and "password" in data and "server" in data:
            return parse_juicity(data, label), text
        if "proxies" in data and isinstance(data["proxies"], list) and data["proxies"]:
            node = parse_clash_proxy(data["proxies"][0], label)
            if node:
                return node, text
            return _mk_node(f"clash/{data['proxies'][0].get('type','?')}", "-", "-", "-", "-", None, [],
                            f"协议 {data['proxies'][0].get('type')} 暂不支持转链接，请看原始内容"), text
        obs = data.get("outbounds")
        if isinstance(obs, list) and obs:
            first = obs[0]
            if first.get("protocol") == "vless":
                return parse_xray(data, label), text
            o = next((x for x in obs if x.get("type") not in SKIP_TYPES), None)
            if o:
                node = parse_singbox_outbound(o, label)
                if node:
                    return node, text
                return _mk_node(f"singbox/{o.get('type')}", "-", "-", "-", "-", None, [],
                                f"协议 {o.get('type')} 暂不支持转链接，请看原始内容"), text
    except Exception as e:
        return _mk_node("?", "-", "-", "-", "-", None, [], f"解析失败：{e}"), text

    return _mk_node("?", "-", "-", "-", "-", None, [], "未识别的配置结构，请看原始内容"), text


# ---------- 本地状态 ----------
def local_state(tool_key: str, slot: int):
    """返回该槽位上次拉取的 (text, 来源说明) 或 (None, None)。"""
    c = read_cached(tool_key, slot)
    if c:
        return c, "上次拉取"
    return None, None


def local_summary(tool_key: str):
    """工具列表里显示的本地概要：该工具所有槽位缓存里最新的一份。"""
    t = TOOLS[tool_key]
    best = None
    for f in CACHE_DIR.glob(f"{tool_key}_ip*"):
        if best is None or f.stat().st_mtime > best.stat().st_mtime:
            best = f
    if best:
        slot = int(re.search(r"_ip(\d+)", best.name).group(1))
        node, _ = parse_config_text(best.read_text(encoding="utf-8", errors="replace"), f"{t['label']}-ip{slot}")
        return dict(node=node, mtime=datetime.fromtimestamp(best.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    source=f"上次拉取(ip{slot})")
    return None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}]", fmt % args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = (PANEL_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
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
                                  badge=badge, local=loc))
            self._json(dict(panel_dir=str(PANEL_DIR), tools=tools))
        else:
            self._json(dict(error="not found"), 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json(dict(error="请求体不是合法 JSON"), 400)

        if self.path != "/api/fetch":
            return self._json(dict(error="not found"), 404)
        tool = body.get("tool")
        slot = int(body.get("slot", 1))
        if tool not in TOOLS:
            return self._json(dict(error=f"未知工具 {tool}"), 400)
        if not (1 <= slot <= TOOLS[tool]["slots"]):
            return self._json(dict(error=f"槽位超出范围，该工具只有 ip1~ip{TOOLS[tool]['slots']}"), 400)
        try:
            text, source = fetch_remote(tool, slot)
            node, _ = parse_config_text(text, f"{TOOLS[tool]['label']}-ip{slot}")
            old_text, old_from = local_state(tool, slot)
            old = None
            if old_text:
                old, _ = parse_config_text(old_text, f"{TOOLS[tool]['label']}-旧")
            changed = None
            if old is not None:
                changed = not (old["server"] == node["server"] and old["port"] == node["port"])
            write_cache(tool, slot, text)  # 每次拉取都更新缓存（冷启动后下一次就有"上次拉取"了）
            return self._json(dict(source=source, node=node, old=old, old_from=old_from,
                                   changed=changed, raw=text, first_time=old is None))
        except Exception as e:
            return self._json(dict(error=str(e)), 502)


def main():
    print(f"ChromeGo 节点面板  ->  http://127.0.0.1:{PORT}")
    print(f"面板目录          ->  {PANEL_DIR}")
    print(f"配置缓存          ->  {CACHE_DIR}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
