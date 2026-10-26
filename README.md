# ChromeGo 节点面板

本地 Web 工具：拉取 ChromeGo 免费节点池各槽位的配置，解析成 v2rayN 可直接导入的分享链接和订阅，支持新旧配置对比与全槽位去重汇总。数据只落在本地 `cache/`。

## 运行

```bash
pip install -r requirements.txt
python server.py
```

打开 http://127.0.0.1:8765 ，订阅地址 http://127.0.0.1:8765/sub 按客户端 User-Agent 自动分流格式：v2rayN 拿到 base64 分享链接列表，Clash Verge / mihomo 系拿到 YAML 配置（含 hysteria v1，不含 naive/juicity）。

## 依赖

仅 Python 标准库 + PyYAML（解析 Clash.Meta 槽位的 YAML 配置），无其他第三方包。

## 说明

- `cache/` 是运行时产物（不入库），启动后会后台预热把缺缓存的槽位拉一遍
- 云端每个编号是固定"槽位"，内容由作者滚动覆盖，拉到的永远是当下最新配置
- **外部公共源**：面板内置 ALIILAPRO、Pawdroid、ermaozi 三个实测存活率靠前的公共订阅源。拉取后会解析全部链接、并行**真握手测活**（TCP → TLS 握手 → ws 发 WebSocket 升级要求 101，4 秒超时），按延迟排序，只把活节点（每源最多 300 个）并入订阅。raw.githubusercontent 不通时自动走 jsDelivr 镜像。**每 40 分钟自动刷新一次**（也可在面板手动点"刷新外部源"，约 1 分钟），某个源拉取失败会在面板上标红显示原因
- 跨源合并后按延迟取**最快 120 条**进订阅：免费节点死得快，列表里塞几百条只会让客户端难用，不如只给最快的
- 源的选择是实测出来的：ALIILAPRO 量大但 87% 是 vmess（复测存活率约 7%）、Pawdroid 只有 13 个但存活率 69%、ermaozi 每日更新且 trojan/ss/vless 三协议互补（复测存活率约 24%）。mahdibland 的 `Eternity.txt` 复测只有 3% 存活、`freefq/free` 已停更两年、`barry-far/V2ray-Configs` 被 GitHub ToS 封禁，都没有接入
- 测活按"非 vmess 优先"取前 600 条：ALIILAPRO 的 vless 全排在文件末尾，不排序等于永远测不到它们（实测同一时刻 600 条 vmess 存活 29 个、全量 2317 条存活 273 个，其中 vless 54 个）
- 外部源只收录 TCP 协议（vmess/vless/trojan/ss）；hysteria 系是 UDP，无法测活，仍来自 ChromeGo 槽位
- **过滤 VMess**（默认开启）：免费源的 vmess 节点通常半小时内就失效（TCP 端口还活着、认证/上游已死），面板可一键过滤，只保留 vless/trojan/ss。切换即时生效，不用重新刷新
- Clash 订阅里的 `♻️ 自动选择` 是 `url-test` 组（2 分钟测一次、100ms 容差、连续 3 次失败才降级），客户端会自动切到当前还活的节点，不用手删

## 免责声明

本项目仅供学习与技术研究之用。节点内容均来自公开网络渠道（ChromeGo 节点池及文中所列公共订阅源），本项目不创建、不存储、不控制任何节点，也不对其可用性与安全性作任何担保。使用本项目时请遵守您所在地区的法律法规，由此产生的一切后果由使用者自行承担。

## 致谢

本面板基于以下项目与资源构建，感谢各位维护者：

- **[bannedbook/fanqiang](https://github.com/bannedbook/fanqiang)** —— 本面板参考的原 ChromeGo 项目（翻墙-科学上网），免费节点池（Xray / Clash.Meta / Hysteria / Hysteria2 / Juicity / NaiveProxy / Sing-box 各槽位配置）来自其维护的更新仓库 [gitlab.com/free9999/ipupdate](https://gitlab.com/free9999/ipupdate)
- **[ALIILAPRO/v2rayNG-Config](https://github.com/ALIILAPRO/v2rayNG-Config)** —— 外部公共源之一，实测规模最大的免费订阅仓库
- **[Pawdroid/Free-servers](https://github.com/Pawdroid/Free-servers)** —— 外部公共源之一，少而精的精选订阅
- **[ermaozi/get_subscribe](https://github.com/ermaozi/get_subscribe)** —— 外部公共源之一，每日自动更新，vless/trojan/ss 三协议互补
