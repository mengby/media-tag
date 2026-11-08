# D17 公共素材检索 API

2026-11-08：`POST /v1/assets/search`、能力发现和请求契约已实现，复用 D15 检索、D06 人工值及 D17 文件权限/隐私来源链。使用项目 `.venv`，无新依赖或数据库迁移。与已有详情、预览及下载合成公共取材接口；MCP、相邻剪辑项目和真实宿主联调仍是后续任务，不能把本机 HTTP 通过当作跨端业务验收。

## 启用与发现

沿用现有私有配置及 `media-tag serve --config ...` 命令，启用 terminology、text、search（见 [D15说明](search-feedback.md)）；公共接口不依赖 web_enabled。Worker 与普通剪辑账号分别配置。剪辑端建议绑定 reader，并显式授予所需素材版本 read；角色和对象范围来自服务验证的凭证，不接受工具参数中的 user_id、role、后端目录或下载 URL。

| 已实现接口 | 内容 |
| --- | --- |
| `GET /v1/capabilities` | 身份可用能力；search_enabled=false 时客户端不能假装支持语义搜索 |
| `GET /v1/search-contract` | 严格 JSON 请求 Schema、版本、错误与分页口径；仅启用 search 时可用 |
| `POST /v1/assets/search` | 字面/发布术语/待选模型候选、硬筛选、按根分组后的固定默认版本分页 |
| `GET /v1/assets/{id}` | 已有固定版本详情、当前修订、人工值/AI建议、来源、技术与可见同组版本 |
| `GET /v1/assets/{id}/preview-manifest?sha256=…` | 已有受权预览，可增加 start_seconds/end_seconds 指定播放范围 |
| `GET /v1/assets/{id}/download-manifest?sha256=…` | 已有固定文件哈希/大小/强ETag/稳定入口，实际验证对象 |
| `GET/HEAD .../content`、`.../preview-content` | 已有完整所选文件，单Range、撤权检查及句柄生命周期 |

下载与预览字节、错误和安全边界见 [文件基础](file-access.md)。清单只返回相对 `/v1/` 入口，不返回后端地址、原始存储 key 或临时签名链接；调用方只能相对事先配置的可信 origin 解析并重新携带自己的凭证。来源页面是数据，不自动抓取。

## 搜索输入

最小请求（已实现；JSON 字段按实际需求选择）：

```json
{"schema_version":"d17-search-v1","query":"需要检索的原话","context":{"direction":"unspecified"},"filters":{"media_kinds":["video"],"min_file_duration_seconds":1.0},"limit":10}
```

query 必填且最多2000字符；limit 默认10、最大50。context 沿用 D14 的已发布科室/器官 ID、肯定/否定、变化方向和上下文要求；未知分类 ID 被拒绝。模型候选不会取第一项自动扩展。调用者选择概念时传 selected_concept_ids 及原响应的 selection_lexicon_release_id，旧版本选择返回409。literal_only 可关闭术语扩展，use_model 可明确关闭模型召回，include_drafts 默认 false。

shot_intent 最多2000字符，保留为取材意图说明；**不自动解释成医学标签或硬条件**。需要限制方向、用途或文件属性时使用 context/filters。它与整个请求一并绑定游标，改变意图后需重新从第一页检索。

filters 的硬条件在服务端执行，未知值不能当作 false、零或合格：

| 字段 | 实际依据 |
| --- | --- |
| media_kinds | 实际 probe/intake 的 image/gif/animation/video/audio |
| format_names | ffprobe 实际格式名称，如 png_pipe、mov、matroska；不是扩展名或任意MIME字符串 |
| min/max_file_duration_seconds | 当前完整文件时长，不是建议片段时长；不生成剪辑以满足条件 |
| min_display_width / min_display_height | 实际显示尺寸，未知排除 |
| has_audio / has_transparent_pixels | 实际技术检查的三值结果；未知不等于“无” |
| usages | 原始“用途大类”的闭合选项，且素材已有对应人工有效值；不根据名字或查询猜用途 |
| exclude_asset_ids | 明确排除的文件版本 ID，最多100个 |
| require_redacted | 必须为登记有 mask 配方且通过当前隐私链门槛的衍生输出；人脸检测报告和未处理原件不能满足 |

无技术硬条件时仅为匹配结果读取技术报告，不扫描全部无关素材报告。采用 D15 当前授权版本预算与明确超预算503，不以部分扫描结果冒充完整无匹配。

## 响应与分页

响应 schema_version=d17-search-v1。state 表示 ready/degraded，result_state 表示 matches/empty；resolution 保留 D14 的明确解析、歧义、无匹配及降级，empty_reason 区分未识别说法、需选概念及识别后无可见素材。降级时仍可返回真实字面命中，不能把服务不可达/失败当无匹配。

items 每根素材只返回一个明确固定版本，含 asset_id/root_asset_id/revision/SHA256/大小、技术信息、人工有效值、实际来源、命中依据、变换及预览/下载入口。默认先排已确认索引，再排有人工有效依据的匹配，再排仅名称/可选AI初稿；同根选对应的最高优先版本。ranking_score 是相对排序分，不是医学正确概率。模型候选仍需选择，不用正确答案替用户做“人工选择”。

technical 新增实际 format_names、has_transparent_pixels、video_codec、frame_rate；frame_rate 是 ffprobe 的平均帧率，不保证恒定帧率，未知仍为 null。既有 time_basis 保留当前文件归一化原点与时长。transform 保留累计倍速、实际时间映射和 apply_transform_again=false；下载的字节已经过登记变换，不再次套用默认速度或打码。

fragments 只来自仍有效的人工概念关联，且该版本有可核验的连续观察区间与共同文件时钟。每条绑定实际 asset_id/hash、半开区间、原点/时长、概念/link、否定/方向及依据类型。没有可靠区间时为空；不是凭稀疏帧/文件名编造的“最佳片段”。不等于指定业务人员已签收，更不等于本次文稿的医学用途批准。

next_cursor 需与原请求完全复用；签名绑定账号、角色、意图/全部筛选、词表/索引/阈值/缓存版本、可见匹配依据与数据修订。范围变化返回409，伪造返回422，服务重启后从第一页重查。返回详情前再检查当前权限、哈希/修订和隐私门槛；中途变化使响应失败，不用另一版本补位。实际内容完整性仍在清单/字节接口重新校验，搜索 metadata 不声称已经读完每个文件。

标签确认、隐私门槛、来源/许可及当前用途分别表示：privacy_gate=passed_for_this_request 只说明该请求满足文件策略，不表示原件已经匿名化；license_status 保守为 unverified，真实来源记录另列；current_use_review=not_checked、medical_use_approved=false。库内索引确认不能作为剪辑用途检查或最终医学审核。

## 指定播放区间

preview-manifest 可同时传 start_seconds/end_seconds，使用当前所选文件的归一化秒，必须有限、0≤start<end≤已知时长。缺少一端/反向/非有限/越界422，缺可靠时钟409。requested_playback 绑定文件/hash/时间基准，并明确 whole_file_bytes=true、server_clip_generated=false、actual_playback_not_attested=true。

原生资源仍是完整所选文件，客户端依据共同原点 seek，不是服务端新转码/裁片；缩略图按实际源时间过滤到请求区间内，缺帧不回退其他区间或原件画面。full_video_observed 始终 false，播放器或模型必须实际观看才可作内容判断。状态重查入口保留相同区间。

## 错误与实际验证

公共接口沿用 d17-files-v1 错误信封。401缺凭证、403角色/对象拒绝、409游标/隐私/播放时钟变化、412固定哈希不符、422输入/上下文/区间错误、503预算/后端/元数据失败；不回显 token、原始异常、文件路径或签名。接口不提供上传、标签修订或词表发布工具。

`tests/search/test_public_api.py` 验证固定版本、权限、输入边界、游标、实际probe硬筛选、人工优先、重新授权及连续片段；既有38项文件测试继续验证字节门槛。真实脚本 `tests/search/validate_public.py` 在隔离本机服务执行：合成PNG/增长视频、真实BGE与CPU、分页/歧义选择/用途格式筛选、片段清单/HEAD/Range、完整SHA256/解码、真实2倍变速副本与单独授权、旧下载/游标撤回。私有报告 `jobs/d17-semantic-84e69da2/validation.json` 记录29次HTTP、9次CPU任务，源模板/字节不变，医学字段/隐私/索引批准0；元数据修订仅因显式人工工程关联和ACL事件改变。

D17 公共 API 工程范围具备；正式词表/独立效果、真实AI宿主与跨机器取材、真实用途审查及部署/并发验收仍待后续。下一步 D18 只读 MCP 适配，不改相邻项目规则或共享素材。

本轮新增7项；项目`.venv`全量585项、纯核心514项、普通wheel集成280项及4项JS通过，三环境pip check与compileall通过。人员页实际Chromium回归见 `jobs/d15-validation-20261108-cddb3e02/validation.json`。74份Markdown链接/代码块及119个wheel模块/资源一致；原数据库迁移不变。
