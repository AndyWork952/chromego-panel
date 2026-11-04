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

# trojan 走 ws 的两种写法（标准 / ermaozi 私有）都要能识别并转成 mihomo 的 ws 配置
tj_ws_std = "trojan://pw@1.2.3.4:443?sni=s.com&type=ws&host=h.com&path=%2Fp#a"
tj_ws_erm = "trojan://pw@1.2.3.4:443?allowInsecure=1&sni=s.com&ws=1&wspath=%2F#b"
assert server._link_ws(tj_ws_std) == ("h.com", "/p")
assert server._link_ws(tj_ws_erm) == ("", "/")
p1 = server._clash_proxy(tj_ws_std, "a")
p2 = server._clash_proxy(tj_ws_erm, "b")
assert p1["network"] == "ws" and p1["ws-opts"] == {"path": "/p", "headers": {"Host": "h.com"}}
assert p2["network"] == "ws" and p2["ws-opts"] == {"path": "/"} and p2["skip-cert-verify"] is True
p3 = server._clash_proxy("trojan://pw@1.2.3.4:443?sni=s.com#c", "c")
assert "network" not in p3  # 纯 trojan 不该多出 ws 字段

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
# 第二个源：只有一个 TCP 节点，验证多源合并时排后面的源不会被挤掉
server.EXT_SOURCES["demo2"] = dict(label="demo2")
tj2 = "trojan://pw@7.7.7.8:443?sni=s.com#tr2"
(server.CACHE_DIR / "ext_demo2.json").write_text(
    json.dumps(dict(fetched="t", nodes=[dict(url=tj2, name="t2", ms=None)])))

# TCP 系测活通过全收：demo 的 v/tj + demo2 的 tj2 都得在，源内顺序不变（延迟不参与排序）
urls = [u for u, _, _ in server.ext_alive()]
assert tuic not in urls and h2b in urls and tj in urls and tj2 in urls  # tuic 不收，其余全留
assert urls.index(v) < urls.index(tj) < urls.index(h1) < urls.index(h2)  # TCP 全收按缓存顺序；hy 同源内顺序不变

# hy1/hy2 免检限量 + 跨源轮流取：名额只给 1 个时第二个源的 hy2 也要能轮到
server.EXT_SOURCES["demo3"] = dict(label="demo3")
server.EXT_KEEP_HY1, server.EXT_KEEP_HY2 = 0, 1
(server.CACHE_DIR / "ext_demo3.json").write_text(
    json.dumps(dict(fetched="t", nodes=[dict(url="hysteria2://auth@3.3.3.3:443?sni=s.com#h2c", name="h2c", ms=None)])))
urls = [u for u, _, _ in server.ext_alive()]
assert set(urls) == {v, tj, ss, tj2, h2}  # TCP 不受影响（v/tj/ss/tj2 全收）；hy2 轮流取第 0 轮取 demo 的 h2（demo2 无 hy2，demo3 的 h2c 下轮才轮到）
server.EXT_KEEP_HY1, server.EXT_KEEP_HY2 = 20, 60
urls = [u for u, _, _ in server.ext_alive()]
assert "hysteria2://auth@3.3.3.3:443?sni=s.com#h2c" in urls  # 名额够时各源 hy2 都进

# 两种订阅格式（外部源 8 条，其中 hy1 只进 Clash 不进 v2rayN）
assert base64.b64decode(server.build_sub()).decode().count("://") == 7
cfg = yaml.safe_load(server.build_sub_clash())
assert [g["type"] for g in cfg["proxy-groups"]] == ["select", "url-test"]
assert len(cfg["proxies"]) == 8

print("selftest OK")
