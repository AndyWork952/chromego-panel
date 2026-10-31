# ChromeGo 节点面板

本地 Web 工具：拉取 ChromeGo 免费节点池各槽位的配置，解析成 v2rayN 可直接导入的分享链接和订阅，支持新旧配置对比与全槽位去重汇总。数据只落在本地 `cache/`。

## 运行

```bash
pip install -r requirements.txt
python server.py
```

打开 http://127.0.0.1:8765 ，订阅地址 http://127.0.0.1:8765/sub 按客户端 User-Agent 自动分流格式：v2rayN 拿到 base64 分享链接列表，Clash Verge / mihomo 系拿到 YAML 配置（含 hysteria v1）。

## 依赖

仅 Python 标准库 + PyYAML（解析 Clash.Meta 槽位的 YAML 配置），无其他第三方包。

## 说明

- 启动时后台预热槽位缓存（`cache/` 是运行时产物，不入库）。云端每个「源N」是固定地址，内容由作者不定期整批覆盖，拉到的永远是当下最新配置，隔几天重拉一次更稳
- **外部公共源**（ermaozi / cbusifabcap / LeilaoMi真测 / ALIILAPRO）：**完全手动更新**，点面板「立即更新」才拉（四源并行下载并测活，总耗时约等于最慢的一个源，通常一分多钟）。**只收录与面板四个槽位类型对应的协议**：vmess / vless / trojan / ss 走 TCP，逐个真握手测活（TLS 节点要完成 SSL 握手，ws 节点还要求 101 升级成功），每源最多送测 800 条，跨源按测活延迟保留最多 120 条；hysteria / hysteria2 走 UDP 无法 TCP 握手测活，免检直收（hy1 最多 20、hy2 最多 60），死活交给客户端的「自动选择」组。跨源去重后进订阅。某个源拉取失败会在面板标红原因，订阅继续用上次的节点。源是 2026-10-31 逐个实测筛选的：ermaozi 每日更新约 28 个 hy2；cbusifabcap 每天 4~6 次更新、130+ 独立 hy2（hy2 主力）；LeilaoMi 每 6 小时用 sing-box 真连接测试 + 国内探针验证（量少但质量高）；ALIILAPRO 每日更新的 2501 节点与前三源仅重合 8 个、TCP 存活率 18.8% 为各源最高（TCP 主力）。Pawdroid 已萎缩到 11 个节点（6 个重合）不再收录。免费节点寿命普遍只有几小时，隔了一夜缓存里的大半已死——**感觉不好使时先点一次「立即更新」再判断**，别急着怪时段或协议
- **外部源三条拉取链路**：GitHub raw → jsDelivr → wget.la 国内反代镜像，逐条尝试。前两条国内直连不通，第三条不开代理也能拉
- **代理只在面板「代理地址」框配置**（存 `proxy.json`，不入库）：填了就只走它（用前真连验证一次，代理进程活着但节点挂了会被识破），失败自动落到直连 + 镜像；留空不做任何代理尝试。只认 HTTP 代理（v2rayN 10808、Clash 7890 都是混合端口，直接填）
- **开代理软件 ≠ 开系统代理**：打开代理软件是在本机开一个代理端口（如 127.0.0.1:10808），等请求主动送进来；「系统代理」开关只是改 Windows 的一条全局设置，给浏览器这类"会来读设置"的程序看。面板不读系统代理设置，只认上面填的地址——决定能不能借道的只有两件事：端口活着、节点真能通
- Clash 订阅里的 `♻️ 自动选择` 是 url-test 组（2 分钟测一次），客户端自动切到还活的节点，不用手删

## 使用建议

博主本机（移动宽带）实测出的时段规律，供参考——网络环境不同，体感可能不一样：

- **白天**：优先用 **hysteria v1** 和 **VLESS（XHTTP + REALITY）** 的节点，白天这两类最稳，速度也在线
- **晚上**：换 **hysteria2** 的节点更顺

这只是当下的观察，不是铁律。节点时好时坏的原因很多（源换了新货、运营商调整、节点寿命到了），不顺的时候先点一次「立即更新」再下结论；客户端的「自动选择」组也会自己挑活节点，懒得手动切换就交给它。

## 免责声明

本项目仅供学习与技术研究之用。节点内容均来自公开网络渠道（ChromeGo 节点池及文中所列公共订阅源），本项目不创建、不存储、不控制任何节点，也不对其可用性与安全性作任何担保。使用本项目时请遵守您所在地区的法律法规，由此产生的一切后果由使用者自行承担。

## 致谢

本面板基于以下项目与资源构建，感谢各位维护者：

- **[bannedbook/fanqiang](https://github.com/bannedbook/fanqiang)** —— 本面板参考的原 ChromeGo 项目（翻墙-科学上网），免费节点池（Xray / Clash.Meta / Hysteria / Hysteria2 / Juicity / NaiveProxy / Sing-box 各槽位配置）来自其维护的更新仓库 [gitlab.com/free9999/ipupdate](https://gitlab.com/free9999/ipupdate)
- **[ermaozi/get_subscribe](https://github.com/ermaozi/get_subscribe)** —— 外部公共源之一，每日自动更新
- **[cbusifabcap/daily_free_vpn](https://github.com/cbusifabcap/daily_free_vpn)** —— 外部公共源之一，每天多次更新，hy2 节点量最大
- **[LeilaoMi/AutoMergePublicNodes-Optimized](https://github.com/LeilaoMi/AutoMergePublicNodes-Optimized)** —— 外部公共源之一，每 6 小时 sing-box 真测 + 国内探针验证
- **[ALIILAPRO/v2rayNG-Config](https://github.com/ALIILAPRO/v2rayNG-Config)** —— 外部公共源之一，每日更新的大池，TCP 系节点存活率最高
