# -*- coding: utf-8 -*-
"""自检（不联网）：python selftest.py
覆盖分享链接解析、去重指纹、外部源多协议收录与分协议限量、两种订阅格式。
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
assert server.link_name(v) == "名"
assert server._parse_link("vmess://!!!") is None  # 坏链接不能抛异常

p = server._clash_proxy("vless://uuid@1.2.3.4:443?security=reality&sni=s.com&pbk=k&sid=ab&type=ws&path=%2Fq#名", "n")
assert p["type"] == "vless" and p["reality-opts"] == {"public-key": "k", "short-id": "ab"}
assert server._clash_proxy("naive+https://u:p@h:443#x", "n") is None  # mihomo 不支持
assert server._clash_proxy(vm(dict(add="1.2.3.4", port="80", net="http", path="/p")), "n")["network"] == "http"
assert server._clash_proxy(vm(dict(add="1.2.3.4", port="443", tls=True, net="tcp", ps="t")), "n")["tls"] is True

# 同 IP 换 uuid 算换节点
a = server._mk_node("vless", "1.2.3.4", 443, "id-a", "", None, [], "")
b = server._mk_node("vless", "1.2.3.4", 443, "id-b", "", None, [], "")
assert server.nodes_fingerprint([a]) != server.nodes_fingerprint([b])

# 测活判定逻辑（不真连网）
v_tcp = vm(dict(add="1.2.3.4", port="443", id="u", net="tcp", tls="tls", sni="s.com", ps="t"))
v_ws = vm(dict(add="1.2.3.4", port="80", id="u", net="ws", path="/p", host="h.com", ps="w"))
trojan = "trojan://pw@1.2.3.4:443?sni=s.com#tr"
assert server._link_tls(v_tcp) == (True, "s.com")
assert server._link_tls(trojan) == (True, "s.com")
assert server._link_tls("ss://YWVzLTI1Ni1nY206cHc=@1.2.3.4:8388#s") == (False, "")
assert server._link_ws(v_ws) == ("h.com", "/p")
assert server._link_ws("vless://u@1.2.3.4:443?type=tcp#x") is None
assert server._probe_link("vmess://!!!") is None  # 解析不了直接判死，不抛异常

# 外部源多协议收录（缓存目录换成临时目录，不碰真实数据）
server.CACHE_DIR = pathlib.Path(tempfile.mkdtemp())
server.EXT_SOURCES = {"demo": dict(label="demo")}
h1 = "hysteria://auth@5.6.7.8:443?sni=s.com#h1"
h2 = "hysteria2://auth@5.6.7.8:444?sni=s.com#h2"
h2b = "hysteria2://auth@9.9.9.9:443?sni=s.com#h2b"
tj = "trojan://pw@7.7.7.7:443?sni=s.com#tr"
ss = "ss://YWVzLTI1Ni1nY206cHc=@8.8.8.8:8388#s"
# 旧版缓存可能混进不收的协议（tuic），ext_alive 必须挡掉
tuic = "tuic://uuid:pw@6.6.6.6:443#bad"
(server.CACHE_DIR / "ext_demo.json").write_text(
    json.dumps(dict(fetched="t", total=6, nodes=[dict(url=v, name="m", ms=None),
                                                 dict(url=h1, name="h1", ms=None),
                                                 dict(url=h2, name="h2", ms=None),
                                                 dict(url=h2b, name="h2b", ms=None),
                                                 dict(url=tj, name="tr", ms=50),
                                                 dict(url=ss, name="s", ms=None),
                                                 dict(url=tuic, name="bad", ms=None)])))
urls = [u for u, _, _ in server.ext_alive()]
assert tuic not in urls and h2b in urls and tj in urls  # tuic 不收，其余全留
assert urls.index(tj) < urls.index(v) < urls.index(h1) < urls.index(h2)  # TCP 系按延迟排（vmess 无 ms 排最后）

# 分协议限量：TCP 系按 EXT_KEEP_TCP，hy1 按 EXT_KEEP_HY1，hy2 按 EXT_KEEP_HY2
server.EXT_KEEP_TCP, server.EXT_KEEP_HY1, server.EXT_KEEP_HY2 = 1, 0, 1
urls = [u for u, _, _ in server.ext_alive()]
assert set(urls) == {tj, h2}  # tcp 桶按延迟截到 1 个（trojan 50ms 优先于无 ms 的 vmess）、hy1 清零、hy2 截到 1 个
server.EXT_KEEP_TCP, server.EXT_KEEP_HY1, server.EXT_KEEP_HY2 = 40, 20, 60

# 两种订阅格式（外部源 6 条，其中 hy1 只进 Clash 不进 v2rayN）
assert base64.b64decode(server.build_sub()).decode().count("://") == 5
cfg = yaml.safe_load(server.build_sub_clash())
assert [g["type"] for g in cfg["proxy-groups"]] == ["select", "url-test"]
assert len(cfg["proxies"]) == 6

print("selftest OK")
