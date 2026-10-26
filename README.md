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
- **外部公共源**：面板内置 ALIILAPRO + Pawdroid 两个实测存活率高的公共订阅源。拉取后会解析全部链接、并行 TCP 测活（3 秒超时）、按延迟排序，只把活节点（每源最多 300 个）并入订阅。raw.githubusercontent 不通时自动走 jsDelivr 镜像。面板上"刷新外部源"可手动更新（约 1-2 分钟），首次启动缺缓存时会自动拉一次
- 外部源只收录 TCP 协议（vmess/vless/trojan/ss）；hysteria 系是 UDP，无法测活，仍来自 ChromeGo 槽位

## 免责声明

本项目仅供学习与技术研究之用。节点内容均来自公开网络渠道（ChromeGo 节点池及文中所列公共订阅源），本项目不创建、不存储、不控制任何节点，也不对其可用性与安全性作任何担保。使用本项目时请遵守您所在地区的法律法规，由此产生的一切后果由使用者自行承担。

## 致谢

本面板基于以下项目与资源构建，感谢各位维护者：

- **[bannedbook/fanqiang](https://github.com/bannedbook/fanqiang)** —— 本面板参考的原 ChromeGo 项目（翻墙-科学上网），免费节点池（Xray / Clash.Meta / Hysteria / Hysteria2 / Juicity / NaiveProxy / Sing-box 各槽位配置）来自其维护的更新仓库 [gitlab.com/free9999/ipupdate](https://gitlab.com/free9999/ipupdate)
- **[ALIILAPRO/v2rayNG-Config](https://github.com/ALIILAPRO/v2rayNG-Config)** —— 外部公共源之一，实测规模最大的免费订阅仓库
- **[Pawdroid/Free-servers](https://github.com/Pawdroid/Free-servers)** —— 外部公共源之一，少而精的精选订阅
