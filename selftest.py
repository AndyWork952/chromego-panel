# -*- coding: utf-8 -*-
"""自检（不联网）：python selftest.py
覆盖分享链接解析、真握手判定依据、去重指纹、外部源过滤开关、两种订阅格式。
"""
import base64
import json
import pathlib
import tempfile

import yaml

import server


def vm(obj):
    return "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()


# vmess 的 base64 体里含 "/"，不能交给 urlparse（会把 netloc 截断）
v = vm(dict(add="1.2.3.4", port="443", id="u", aid="0", net="ws", path="/p",
            host="h.com", tls="tls", sni="s.com", ps="名"))
assert server._vmess_json(v)["ps"] == "名"
assert server._parse_link(v) == ("vmess", "1.2.3.4", 443)
assert server._link_tls(v) == (True, "s.com")
assert server._link_ws(v) == ("h.com", "/p")
assert server.link_name(v) == "名"
assert server._parse_link("vmess://!!!") is None  # 坏链接不能抛异常

r = "vless://uuid@1.2.3.4:443?security=reality&sni=s.com&pbk=k&sid=ab&type=ws&path=%2Fq#名"
assert server._link_tls(r) == (True, "s.com")
assert server._link_ws(r) == ("", "/q")
assert server._parse_link("vless://u@1.2.3.4:80?type=tcp#x") and server._link_ws("vless://u@1.2.3.4:80?type=tcp#x") is None

p = server._clash_proxy(r, "n")
assert p["type"] == "vless" and p["reality-opts"] == {"public-key": "k", "short-id": "ab"}
assert server._clash_proxy("naive+https://u:p@h:443#x", "n") is None  # mihomo 不支持
assert server._clash_proxy(vm(dict(add="1.2.3.4", port="80", net="http", path="/p")), "n")["network"] == "http"
assert server._clash_proxy(vm(dict(add="1.2.3.4", port="443", tls=True, net="tcp", ps="t")), "n")["tls"] is True

# 同 IP 换 uuid 算换节点
a = server._mk_node("vless", "1.2.3.4", 443, "id-a", "", None, [], "")
b = server._mk_node("vless", "1.2.3.4", 443, "id-b", "", None, [], "")
assert server.nodes_fingerprint([a]) != server.nodes_fingerprint([b])

# 外部源缓存 + vmess 过滤开关（缓存目录换成临时目录，不碰真实数据）
server.CACHE_DIR = pathlib.Path(tempfile.mkdtemp())
server.EXT_SOURCES = {"demo": dict(label="demo")}
(server.CACHE_DIR / "ext_demo.json").write_text(
    json.dumps(dict(fetched="t", nodes=[dict(url=v, name="m", ms=300), dict(url=r, name="r", ms=20)])))

server._ext_cfg["drop_vmess"] = True
assert [u for u, *_ in server.ext_alive()] == [r]
server._ext_cfg["drop_vmess"] = False
assert [ms for _, _, ms, _ in server.ext_alive()] == [20, 300]  # 快的排前面，订阅里先出现

# 两种订阅格式
assert base64.b64decode(server.build_sub()).decode().count("://") == 2
cfg = yaml.safe_load(server.build_sub_clash())
assert [g["type"] for g in cfg["proxy-groups"]] == ["select", "url-test"]
assert len(cfg["proxies"]) == 2

# 跨源合并会按 EXT_KEEP_TOTAL 截断（不能让订阅长到客户端列表没法用）
server.EXT_KEEP_TOTAL = 1
assert len(server.ext_alive()) == 1
server.EXT_KEEP_TOTAL = 120

# hysteria2 是 UDP，测不了活 -> 免检直收、排在最后、不吃 TCP 那 120 条的额度
h = "hysteria2://auth@1.2.3.4:443?sni=s.com#q"
(server.CACHE_DIR / "ext_demo.json").write_text(json.dumps(dict(fetched="t", nodes=[
    dict(url=v, name="m", ms=300), dict(url=r, name="r", ms=20), dict(url=h, name="q", ms=None)])))
assert [u for u, *_ in server.ext_alive()] == [r, v, h]
server.EXT_KEEP_TOTAL = 1
assert [u for u, *_ in server.ext_alive()] == [r, h]

print("selftest OK")
