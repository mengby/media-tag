# 固定服务客户端

仅连接显式配置的 HTTPS 或本机 loopback origin；凭证来自本地私有文件，不来自工具参数。只读取材调用逐次确认 reader 能力，拒绝跳转/任意URL与无界响应，不读取相邻项目配置。D18 MCP 与 D19 适配共用；见 [MCP说明](../../../docs/mcp-adapter.md)。

`models.py` 定义当前用途、获取与检查契约，`store.py` 限定新任务目录/实际句柄/锁/不可变记录，`provider.py` 实现固定字节断点下载、校验/解码、来源登记与预览交接。只生成本任务记录，不接管相邻项目的采用或导出；见 [D19说明](../../../docs/editing-provider.md)。
