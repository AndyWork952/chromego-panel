# FreeProxy 节点面板

本地 Web 工具：拉取 ChromeGo 免费节点池各槽位的配置，解析成 v2rayN 可直接导入的分享链接和订阅，支持新旧配置对比与全槽位去重汇总。数据只落在本地 `cache/`。

## 下载即用（推荐小白）

不用装 Python：到 [Releases](../../releases) 下载对应平台的单文件，双击即可（自动打开浏览器）：

| 平台 | 文件 | 说明 |
|---|---|---|
| Windows | `FreeProxyPanel-windows-x64.exe` | SmartScreen 首次提示"未知发布者"时点「更多信息 → 仍要运行」 |
| macOS (Intel) | `FreeProxyPanel-macos-x64.zip` | 解压后双击使用；若提示"已损坏/无法验证"，终端执行 `xattr -d com.apple.quarantine FreeProxyPanel-macos-x64` 再双击 |
| macOS (Apple Silicon) | `FreeProxyPanel-macos-arm64.zip` | 同上 |
| Linux | `FreeProxyPanel-linux-x64` | `chmod +x` 后运行；CentOS 7+ / Ubuntu 18.04+ / Debian 10+ 通用 |

`cache/`、`proxy.json` 等数据始终生成在程序文件旁边，升级时换个新文件即可，数据不丢。

## 运行（源码方式）

```bash
pip install -r requirements.txt
python server.py
```

打开 http://127.0.0.1:8765 ，订阅地址在面板「订阅地址 · 全部来源合并去重」区展示（形如 `http://127.0.0.1:8765/sub/<随机密钥>`），按客户端 User-Agent 自动分流格式：v2rayN 拿到 base64 分享链接列表，Clash Verge / mihomo 系拿到 YAML 配置（含 hysteria v1）。地址含访问密钥，只填自己的客户端，不要外传；不带密钥的老地址 `/sub` 返回 403。

## 自己打包

在目标平台上执行 `pip install pyinstaller pyyaml && python build.py`，产物在 `dist/`。三平台自动构建见 `.github/workflows/build.yml`（Actions 手动触发或推 `v*` 标签）。

## 依赖

仅 Python 标准库 + PyYAML（解析 Clash.Meta 槽位的 YAML 配置），无其他第三方包。

## 说明

- 启动时后台预热槽位缓存（`cache/` 是运行时产物，不入库）。云端每个「源N」是固定地址，内容由作者不定期整批覆盖，拉到的永远是当下最新配置，隔几天重拉一次更稳
- **外部公共源**（ermaozi / cbusifabcap / LeilaoMi真测 / ALIILAPRO / V2RayAggregator / barry-far，共六源）：**完全手动更新**，点面板「立即更新」才拉（六源并行下载并测活，总耗时约等于最慢的一个源，通常两三分钟）。**只收录与面板四个槽位类型对应的协议**：vmess / vless / trojan / ss 走 TCP，逐个真握手测活（TLS 节点要完成 SSL 握手，ws 节点还要求 101 升级成功），每源最多送测 1500 条、送测顺序按协议优先级（trojan/vless 最先、vmess 次之、ss 最后，避免好协议被上限截断）；跨源去重后：vmess / vless（含 reality）/ trojan **全收、不限量**（截断等于抽签——好节点在源文件里的排序每天变，任何上限都会随机杀掉优质线路；延迟只作展示、不参与排序），**ss 限量 30 条**（公共源里 ss 大量是废弃节点、通过率 ~2%，全收会淹没订阅）；hysteria / hysteria2 走 UDP 无法 TCP 握手测活，免检直收（hy1 最多 20、hy2 最多 30），ss/hy 的死活交给客户端的「自动-全部」组。某个源拉取失败会在面板标红原因，订阅继续用上次的节点。源是 2026-10-31 / 11-04 逐个实测筛选的：ermaozi 每日更新约 28 个 hy2；cbusifabcap 每天 4~6 次更新、130+ 独立 hy2（hy2 主力）；LeilaoMi 每 6 小时用 sing-box 真连接测试 + 国内探针验证（量少但质量高）；ALIILAPRO 每日更新的 2501 节点与前三源仅重合 8 个、TCP 存活率 18.8% 为各源最高（TCP 主力）；V2RayAggregator 聚合器大池 4300+ 条（ss/vmess/trojan 为主，补 ss 系）；barry-far 3400+ 条（vless 2400+ 补 vless/REALITY）。Pawdroid 已萎缩到 11 个节点（6 个重合）不再收录。免费节点寿命普遍只有几小时，隔了一夜缓存里的大半已死——**感觉不好使时先点一次「立即更新」再判断**，别急着怪时段或协议
- **外部源三条拉取链路**：GitHub raw → jsDelivr → wget.la 国内反代镜像，逐条尝试。前两条国内直连不通，第三条不开代理也能拉
- **代理只在面板「代理地址」框配置**（存 `proxy.json`，不入库）：填了就只走它（用前真连验证一次，代理进程活着但节点挂了会被识破），失败自动落到直连 + 镜像；留空不做任何代理尝试。只认 HTTP 代理（v2rayN 10808、Clash 7890 都是混合端口，直接填）
- **开代理软件 ≠ 开系统代理**：打开代理软件是在本机开一个代理端口（如 127.0.0.1:10808），等请求主动送进来；「系统代理」开关只是改 Windows 的一条全局设置，给浏览器这类"会来读设置"的程序看。面板不读系统代理设置，只认上面填的地址——决定能不能借道的只有两件事：端口活着、节点真能通
- Clash 订阅分了「精选 / 全部」两层，都在 `freeproxy-panel` 组里：`精选` 只放真握手验证过的 TCP 系节点（默认走 `自动-精选`），`全部` 含免检的 hy 和仅端口连通的节点（`自动-全部`）。两个 url-test 组都是 2 分钟测一次、`lazy` 只在被选中时测，客户端自动切到还活的节点，不用手删

## 使用建议

面板不是「造节点」，而是把散落各处的免费节点**拉下来、验一遍、合并去重**再给你——省掉自己到处找订阅、一个个试的功夫：

| 你关心的 | 我们的面板 | 直接订阅免费源 |
| --- | --- | --- |
| 国内能不能拉到 | ✅ 三条链路（raw / jsDelivr / wget.la）自动切换，打不开就换镜像 | ❌ GitHub raw 常被掐，得先能翻墙 |
| 节点可用率 | ✅ 逐个真连验活（TLS 握手 + ws 升级），死的先剔除 | ❌ 一堆死节点，得自己一个个试 |
| 多源合并去重 | ✅ 六源 + ChromeGo 一个订阅 | ❌ 手动加 N 个订阅，重复节点自己看 |
| 自动选优 | ✅ 精选/全部 分层，客户端 url-test 自动切活节点 | ❌ 手动挑，选到死节点再换 |

免费节点寿命普遍只有几小时，感觉不好使了先点一次「立即更新」——多半是过期了，不是协议、时段或你网络的问题；客户端的「自动-精选」组也会自己挑活节点，懒得手动切就交给它（挑不出活的再切「自动-全部」试 hy 系）。

## 免责声明

本项目仅供学习与技术研究之用。节点内容均来自公开网络渠道（ChromeGo 节点池及文中所列公共订阅源），本项目不创建、不存储、不控制任何节点，也不对其可用性与安全性作任何担保。使用本项目时请遵守您所在地区的法律法规，由此产生的一切后果由使用者自行承担。

## 致谢

本面板基于以下项目与资源构建，感谢各位维护者：

- **[bannedbook/fanqiang](https://github.com/bannedbook/fanqiang)** —— 本面板参考的原 ChromeGo 项目（翻墙-科学上网），免费节点池（Xray / Clash.Meta / Hysteria / Hysteria2 / Juicity / NaiveProxy / Sing-box 各槽位配置）来自其维护的更新仓库 [gitlab.com/free9999/ipupdate](https://gitlab.com/free9999/ipupdate)
- **[ermaozi/get_subscribe](https://github.com/ermaozi/get_subscribe)** —— 外部公共源之一，每日自动更新
- **[cbusifabcap/daily_free_vpn](https://github.com/cbusifabcap/daily_free_vpn)** —— 外部公共源之一，每天多次更新，hy2 节点量最大
- **[LeilaoMi/AutoMergePublicNodes-Optimized](https://github.com/LeilaoMi/AutoMergePublicNodes-Optimized)** —— 外部公共源之一，每 6 小时 sing-box 真测 + 国内探针验证
- **[ALIILAPRO/v2rayNG-Config](https://github.com/ALIILAPRO/v2rayNG-Config)** —— 外部公共源之一，每日更新的大池，TCP 系节点存活率最高
- **[mahdibland/V2RayAggregator](https://github.com/mahdibland/V2RayAggregator)** —— 外部公共源之一，聚合器大池，ss/vmess/trojan 为主
- **[barry-far/V2ray-Config](https://github.com/barry-far/V2ray-Config)** —— 外部公共源之一，每日更新大池，vless/REALITY 为主
