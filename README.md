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
