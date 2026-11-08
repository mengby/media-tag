# D18 Codex宿主联调

2026-11-08：用户选择当前电脑、Codex客户端，剪辑项目为`D:/ai/auto-video`；本轮只实施D07本机部署和D18宿主联调。已安装全局`media_tag`入口，并用真实已安装Codex CLI完成stdio/工具/图像及停服恢复验证。Codex CLI宿主交付完成；当前桌面会话是否已重载没有通过UI验证。

## 实际配置

本机Codex CLI版本为`0.167.0-alpha.3.2`，沿用当前已登录账号及模型设置，没有读取或复制登录凭证。使用项目`.venv`、已固定官方SDK2.3.0和四个只读工具，仍调用 [D17公共API](asset-search-api.md)。

实际全局配置位于`C:/Users/EDY/.codex/config.toml`，新增独立`mcp_servers.media_tag`，原node_repl及其他配置保留。等价条目见 [配置示例](../config/codex-mcp.example.toml)。命令已实际执行：

```powershell
codex mcp add media_tag --env PYTHONUTF8=1 -- D:\ai\media-tag\.venv\Scripts\python.exe -m media_tag mcp-stdio --config D:\ai\media-tag\data\local\reader-client.local.json
codex mcp get media_tag --json
```

客户端配置只保存`http://127.0.0.1:8765`和绝对reader-token文件路径，不把令牌写入Codex参数、环境或Git。令牌文件由实际用户EDY/SYSTEM ACL保护；服务端只将该身份映射为reader。MCP可以发现search_assets/get_asset/get_asset_preview/get_asset_download，无法上传、发布词表或确认标签/隐私。服务停止时桥接仍可初始化，实际HTTP调用明确返回upstream_unavailable，不返回no_match，不换身份或源文件。

本轮实际传输为本机stdio，不包含远程HTTP/OAuth、公网或云端到内网接入。原 [MCP适配说明](mcp-adapter.md) 的预算、固定版本、模型候选及来源/隐私边界继续有效。

## 实际宿主证据

`tests/mcp_adapter/validate_codex.py` 用真实Codex可执行文件、当前登录及项目`.venv`，执行ephemeral只读检查；不启动实现子代理、不编辑项目或相邻任务。验证用隔离D17工程库，通过本次CLI命令覆盖的仅是该次media_tag客户端配置位置，永久全局入口仍指向实际本机空库。

最终批次 `jobs/d18-codex-host-6ab18384/validation.json`：真实BGE/CPU工程夹具及Codex5次MCP调用，覆盖全部4个工具。Codex实际得到固定版本详情/预览/下载，撤权素材返回file_access_denied/403。父进程实际下载并校验SHA256后将对应PNG作为Codex图像输入；实际回答识别出白底红色圆形。没有声称看过视频、当前剪辑用途通过或医学批准。events.jsonl记录真实mcp_tool_call，不是模型自述调用；其中撤权项的structured_content实际包含403。令牌在宿主输出/诊断/回答中均未出现。

`tests/deployment/validate_installed.py` 进一步使用真实EDY计划任务和永久全局入口：停服时Codex实际MCP调用失败并返回可重试upstream_unavailable；任务重启后，另一次真实Codex调用返回正常空库items=[]。两次运行没有覆盖全局MCP参数，前后全局配置摘要相同；最后服务保持运行。实际报告 `jobs/d07-installed-b4e0cd42/validation.json`。共3次真实Codex只读运行、7次实际MCP调用。

## 桌面端使用

[OpenAI官方MCP说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)说明本地客户端共享Codex配置，并提供桌面设置中的MCP服务器重启步骤。当前桌面会话若未发现新增工具，请在设置的MCP服务器页面找到media_tag并重启连接，再使用刷新后的会话。这个UI动作没有由本轮自动执行；Computer Use技能明确限制对宿主自身UI的自动控制，不能把CLI结果写成已观察到桌面连接重载。

当前空库可以验证连接和错误区分，正式词表/素材尚未导入。完成后续D16业务评测、D19相邻项目来源适配及D20真实剪辑任务时，仍需已授权素材、实际稿件与业务检查；本轮没有更改剪辑项目preview_only、许可策略、素材优先级或共享assets。
