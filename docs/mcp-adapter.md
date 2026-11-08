# D18 只读 MCP stdio 适配

2026-11-08：官方SDK stdio桥接及标准客户端工程已验证。用户随后确定当前电脑与Codex，现已完成实际Codex CLI宿主联调及全局入口配置，见 [Codex说明](codex-host.md)；桌面当前会话重载未自动验证。相邻项目没有修改。桥接调用现有D17 API，不运行另一套检索/医学规则，不直接访问数据库、后端存储或共享素材。

## 依赖与入口

选择并固定官方 `mcp==2.3.0` / `mcp-types==2.3.0`，以官方 SDK 管理协议和进程传输。该版本来源及接口依据为 [官方发布记录](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)、[SDK低层Server接口](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/) 和 [stdio客户端说明](https://py.sdk.modelcontextprotocol.io/client/transports/)。使用低层 Server 是为了直接复用 D17 严格 JSON Schema；参数仍由本项目 Pydantic JSON 契约完整校验，没有自行实现 JSON-RPC/握手。

使用项目 `.venv`。`requirements-mcp.lock` 是 Windows x86_64 / CPython3.12 的44包开发闭包；原核心27包锁不变，增加17包，共12,138,640下载字节，最大是约6.9MB pywin32，其次约3.8MB cryptography。逐wheel对照官方PyPI发布URL、大小和SHA256后离线安装。已安装的62个包/项目版本均未改变，不重新下载 PyTorch/CLIP/BGE。其他机器/平台需单独验证锁和宿主，不直接复制本机 `.venv`。

```powershell
.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements-mcp.lock
.venv\Scripts\python.exe -m media_tag mcp-stdio --config config/mcp-client.local.json
```

第一条在联网新环境仍需取得正式锁定wheel；本次本机已离线复现。原生 `.[mcp]` 是可选生产依赖组，只有显式启用才需要 SDK；基础服务/CLI导入不加载 SDK、不启动桥接或网络连接。stdio 命令已实现，宿主应启动它并通过 stdin/stdout 交换协议，stdout 不作为日志使用。关闭标准客户端后，本机测试的桥接子进程均已退出。

## 私有客户端配置

`config/library-client.example.json` 是**拟定填写示例**，保留域名/凭证位置占位，不是已配置或已部署服务。复制为被忽略的 `config/mcp-client.local.json`，填写批准的服务 origin 和绝对凭证文件路径。token文件只包含该服务 reader 凭证（最多4KiB），不从相邻 `.env`、工具参数或宿主环境猜取。修改凭证文件后重启桥接。

允许 HTTPS；开发时允许 localhost/127.0.0.1/::1 的 HTTP。拒绝带账号密码/路径/查询的 origin、环境代理、重定向和任意来源 URL。响应默认上限4MiB、可配置不超过16MiB；每次网络操作设定超时。错误只携带有界代码/状态/retryable，不透传后端正文、异常、路径或凭证。最多4个同时进行的客户端HTTP操作，忙时明确返回可重试错误。

每个工具调用都重新查询 `/v1/capabilities`，要求实际 reader 身份及相应能力，**不能把 editor/admin 凭证作为桥接凭证**。对象权限和隐私来源链仍在 API 每次请求中检查；read-only 注解是给宿主的提示，实际权限不依赖它。配置中没有凭证原文，工具schema里没有 user_id、role、base_url、token、文件路径或任意HTTP调用。

宿主启动参数的通用形态是：绝对项目 `.venv/Scripts/python.exe`，参数 `-m media_tag mcp-stdio --config <绝对私有配置路径>`。这是通用启动形态，不是所有宿主共用的配置格式。本机Codex已按新宿主说明安装并验证；其他宿主按各自支持的传输、协议及鉴权要求联调。本轮没有开放远程HTTP MCP、OAuth网关或公网监听。

## 四个工具

| 工具 | 严格输入 | 实际行为 |
| --- | --- | --- |
| search_assets | D17 AssetSearch 全部原字段，query/context/filters/候选选择/游标等 | 只调用 POST /v1/assets/search，保留版本、歧义、降级、命中理由和使用边界 |
| get_asset | asset_id | 只读固定文件版本详情与当前索引/来源状态 |
| get_asset_preview | asset_id、sha256；可同时给 start_seconds/end_seconds | 返回固定哈希的预览清单与可核验播放范围；完整文件字节，不生成新裁片 |
| get_asset_download | asset_id、sha256 | 返回核验后的固定下载清单，实际落盘留给任务内下载器 |

列表正好4个，注解 read_only_hint=true、open_world_hint=false。不注册上传、标签修订、词表发布、取任意URL/路径或执行命令的工具。未知工具和额外参数明确报错，不静默解释为其他行为。

工具返回原 API 对象作为 structured_content，另提供等价 JSON 文本；来源文字/URL和素材说明始终是数据，不是可执行指令。MCP输出不包含后端路径，不生成假的 local_path。模型候选仍需明确选择，分数不变成医学正确概率；标签确认、隐私门槛、来源/许可及当前文稿用途检查仍分开。

预览清单/缩略图链接不代表模型已观看，更不能证明连续变化。whole_file、时间原点、实际范围与 transform/apply_transform_again=false 原样保留；已经2倍速的副本不会在桥接中再变速。清单可读不等于实际下载成功、文件适合本次文稿或获医学/许可批准。

## 失败与验证

`is_error=true` 的结构化错误区分 invalid_tool_input、unknown_read_only_tool、reader_credential_required、API对象/哈希/隐私拒绝、upstream_timeout/unavailable、响应预算及不支持的能力。它们不冒充 no_match，也不回退原件、其他身份/URL或其他素材。坏配置的 CLI 只在 stderr 给 mcp_bridge_failed，stdout 无原始异常/凭证。协议级错误由 SDK 处理。

11项新增测试覆盖来源配置、严格输入、角色/错误/预算、两代SDK客户端及真正stdio子进程/HTTP边界，另有纯核心无SDK客户端回归。真实脚本 `tests/mcp_adapter/validate_stdio.py` 复用 D17 隔离几何/视频批次并增加5个真实stdio子进程、23次MCP工具调用及实际CPU BGE召回；与29次直接API请求共64次HTTP。验证了 `2026-07-28` 和 `2025-11-25`，不声称其他协议/真实AI宿主已测。

最终私有报告在 `jobs/d18-mcp-3196c2b2/validation.json`：5个子进程已退出，非法参数/写工具/无权原件/不合格凭证/离线分别报错；语义候选没有自动选择，2倍速/副本独立权限保留；MCP调用前后素材修订、人工值、任务及发布指针不变。原素材和模板字节未变，无医学字段、隐私或索引批准。实际AI观看与业务验收均为false，SDK测试不代替宿主I01/I02/I08签收。

D18后续交付：适配工程与真实Codex CLI宿主已验证，永久入口配置/停服恢复已通过，详见 [实际宿主说明](codex-host.md)。D19本仓库客户端已实现，实际剪辑仓库接入仍单独明确维护范围；不修改共享assets或既有任务的预览/导出策略。下方596项为原适配工程阶段记录，本轮部署/宿主验证另见新说明。

本轮项目`.venv`全量596项（含11项新增）通过，最后凭证字节预算补验11项通过；独立44包普通wheel环境83项、无SDK核心环境10项通过，pip check/compileall通过。原始62个包/项目版本核对不变。
