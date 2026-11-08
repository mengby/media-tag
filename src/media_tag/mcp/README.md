# D18 只读 MCP 桥接

固定官方 SDK，仅暴露 search_assets/get_asset/get_asset_preview/get_asset_download 四个只读工具。SDK管理stdio/协议，业务复用D17，不实现另一个搜索器，不接受任意路径/URL或身份参数。实际AI宿主配置仍待用户确定；标准客户端工程验证不代替宿主业务验收。见 [说明](../../../docs/mcp-adapter.md)。
