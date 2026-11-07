# D09 上传与衍生契约

日期：2026-11-07。状态：断点上传、实际文件校验、私有登记/分析 outbox、衍生配方/时间映射、受信输出登记与授权版本列表已实现。使用项目 `.venv`，无新增第三方依赖，沿用 27 包哈希锁。对应 D09、R01/R04/R09/R10、N01/N02/N03/N05，覆盖 A02/A03/A09 的工程前置；D10已实现[打码核心与复核接口](face-redaction.md)，D11[固定变速/组合](fixed-retime.md)已实现，D12页面未实现；D17[授权预览/下载基础](file-access.md)已实现，整项语义验收仍未完成。

## 上传协议与恢复

上传使用 JSON 建立会话、原始二进制分块，不要求 multipart 依赖。沿用服务端 bearer 身份，客户端不能指定 owner、角色、实际存储 key 或审核状态。Worker 无上传/计划/发布权限。

| 方法与路径 | 请求与行为 |
| --- | --- |
| `POST /uploads` | UploadSpec：request_id、显示用 filename、size_bytes、完整 sha256；可选 source、purpose、known_asset_id |
| `GET /uploads/{id}` | 本人/admin；返回状态、已提交 offset、分块上限、asset_id、job_id、安全错误码、处理历史口径 |
| `PUT /uploads/{id}/chunks?offset=N` | 原始 body，`X-Chunk-SHA256` 必填；从当前已提交偏移追加 |
| `POST /uploads/{id}/complete` | 校验、私有登记，再尝试派发持久化待分析事件 |
| `POST /uploads/{id}/cancel` | 关闭未完成会话、递增 fencing epoch，旧完成执行不能登记 |
| `POST /variants/plans` | `{request_id, spec}`，PlanSpec 见下文；不启动不存在的渲染阶段 |
| `GET /variants/plans/{id}` | 计划 owner/admin，仍检查请求人的源访问权限 |
| `GET /assets/{id}/variants` | 对组内每个版本分别鉴权，仅列可见版本，无文件路径或签名链接 |

上传会话由 owner＋request_id 幂等；同 ID 更改元信息拒绝。相同 offset/大小/分块哈希的重发幂等，冲突、跳跃、空块、超长和分块校验失败均拒绝。GET 返回数据库已提交偏移，进程/服务重启后照此续传。数据库与块发布失败不会推进偏移；孤立块须重新验哈希才可复用。

状态为 `receiving → validating → completed`，另有 `rejected/cancelled`。完成执行默认租约 300 秒；同一租约内只允许一个 finalizer，超时可接管并递增 epoch。旧 finalizer、取消后的执行都不能登记。存储/工具临时故障和数据库失败允许重试完成；坏哈希/坏媒体拒绝，须修正后开新会话。未完成、格式错误、执行失败和未知历史分别保留，不把这些写成“无匹配”。

默认单文件最大 2GiB，单块 4MiB（配置最大 8MiB），每人最多 8 个进行中会话，声明暂存总额默认 4GiB。`LocalConfig.upload_options` 可收紧/调整。总额保守计算所有保留会话，包括已完成、拒绝、取消；关闭会话不等于磁盘已清理。当前保留块、失败尝试和审计，不自动删除用户数据；正式保留期、磁盘容量和管理员清理工具留 D07，不通过反复取消绕开暂存额度。

文件名仅作受校验的叶子显示名，不拼接为写入路径。私有目录为 `jobs/upload-<uuid>/parts/<chunk-sha256>` 和 `input-<epoch>.bin`；目录拒绝链接/重解析点，块与合并尝试不覆盖旧文件。来源原件从未写入或移动。暂存与对象库不可重叠，CAS 通过原有不可变后端复制/验哈希。

## 完成校验、去重与后台事件

服务端逐块验真实 SHA256，再核对合并大小/完整 SHA256。D02 工具识别实际格式，并要求与支持的扩展名族一致；随后做受限实际解码。支持 PNG/JPEG/GIF/WebP/BMP/TIFF、MP4/MOV/M4V/MKV/WebM/AVI/TS/MPEG，以及 WAV/MP3/FLAC/Ogg/AAC/M4A。未知扩展名、伪装格式、损坏文件或超预算均拒绝。上传采用短窗口解码，**不是全文件完整性、隐私或语义验收**；输出登记另做完整受预算解码。

素材登记、来源记录、会话完成与 outbox 事件在同一 SQLite 事务提交。对象文件先写私有不可变库；事务回滚可能留下无引用对象，不能把它当已登记素材。此处不会删除其他素材或覆盖上一版本。

同内容在对象库只保留一个 blob，不因哈希相同而合并不同账号/请求的素材、来源或权限。已完成请求重复 complete 返回同一 asset；新上传通常有自己的来源记录。许可仍为 `unverified`，素材状态仍为 `ai_draft`，不生成医学/人工确认。

outbox 先持久化，再按固定 event_id 幂等创建 Router job；进程丢失提交回执后可恢复，不重复建任务。HTTP complete 尝试派发，应用启动（含 work-once）也扫描；没有新的常驻线程。`library` 当前只自动安排真实可用的 probe→evidence，`processing` 仅安排 probe，不依赖语义标签。模型建议待 D04 接入，不伪造 OCR/CLIP/医学结果。派发失败不回滚已上传素材，返回 dispatch_error，事件保留待恢复。

重新上传本库衍生内容时，只在当前账号可访问的衍生版本中匹配；唯一匹配复用原 asset/lineage/隐私门槛，多义需明确 known_asset_id。显式 known_asset_id 同样要鉴权并匹配完整字节。新上传的 source 说明保留在该用户的上传 receipt，不借只读权限改写旧记录。命中衍生输出不重新走原件分析入口。其他账号即使上传相同原始字节，也不会获知隐藏版本/关系；它的独立私有导入标为 `external_unknown`，不声称知道外部文件曾否变过速。

## 衍生契约与时间基准

PlanSpec 固定 `parent_asset_id`、`expected_revision`、Recipe，可显式 `chain_from_derived`。Recipe 为白名单：speed 默认 1.0、范围 0.25～4；音频为 preserve_pitch/drop，允许指定现存音轨 index；mask 只记录轨迹 revision、mosaic/solid 和强度；编码固定 H.264/MP4，crf 18～28、preset veryfast/medium。拒绝 shell/任意 FFmpeg 参数与额外字段。renderer_revision/model_revision 纳入配方哈希；计划可暂存未配置版本，但登记时必须绑定实际执行版本，不能用默认空版本冒充已渲染。mask 契约不表示检测/跟踪或打码已完成。

创建计划会通过当前授权和固定源哈希实际探测。当前限单视觉流视频，选中流必须有已知起点/时长；多音轨必须明确选择或 drop。共同原点取选中流最早起点，时长由选中流端点跨度计算，不把容器的绝对结束时间当作视频时长。记录源原点、原生 time_base 分数、各流偏移/时长，以及规范化 FixedTimeMap；缺少可靠时钟时明确拒绝，不猜零点。对源规范化时间 t，固定速度 s 的目标位置为 t/s，端点按实际输出容差验证。

源 SHA256、规范化配方、软件契约版本、源时钟/流选择及累积变换形成 recipe_hash；owner＋parent＋完整 hash 去重。每次请求仍验证源权限和 revision，同 request_id 改输入拒绝。更改速度默认从用户指定的固定源版本重新规划；选择衍生输入必须显式确认链式处理，读取已登记来源/配方并记录 cumulative_speed，禁止隐式在上次输出叠加。外部导入未知历史不伪造累积倍率。

## 受信输出登记与权限

`VariantService.register_output` 是服务侧受信采用接口，不是 HTTP Worker 发布接口。基础服务默认output_stages为空；D10独立受信服务显式绑定redaction_render，成功且已停止后采用真实打码结果。D11受信服务绑定retime_render；两者均保留原有租约/哈希校验。测试显式注册 `synthetic-render`，验证协议而不冒充产品渲染器。

登记必须由 admin 服务能力调用，绑定计划 owner/parent/source_sha256、已注册生产阶段、包含 plan_id/recipe_hash 的固定任务配置，以及 Router 已采用、当前 generation/epoch、成功且已真实释放资源的结果。任务取消、旧 generation、未采用或不属于结果的 artifact 均拒绝。实际校验输出路径/哈希、MP4 族/H.264、单视频及所选音频布局、完整解码、尺寸、规范化起点、各流时长/偏移与倍速映射。夹带额外视频流不能登记。

在重新检查源可读和执行状态后，单事务写新 asset/blob 位置、root/parent、source/output SHA256、recipe_hash、time_map、验证记录和真实登记操作者审计。来源通过 variant_source_links 引用原记录，不编造新许可/署名；标签不自动继承或确认。失败回滚新记录，不损坏既有版本；同计划/同输出重复登记复用，不同输出冲突。

每个衍生输出默认私有、独立授权；版本列表逐条过滤，不因拥有一个版本而看到原件或其他账号版本。mask输出以及带隐私来源链的新变体privacy_status为pending，其余为not_applicable；D11在计划和采用时强制保留来源链要求；D09 没有“隐私通过”操作。pending 内容仅允许 owner 的内部规划读取，不因授予普通 read 就公开预览。通用 Catalog.open_content 仍拒绝衍生文件，实际文件能力查D17的 /v1 清单；D10新增不可变复核事件，已批准的具体输出在版本列表可标download_available=true，批准/撤回绑定实际输出哈希及配方；失败不会回退原件。

## 实际验证与待办

新增 51 项 D09 测试，全量 239 项：实际 PNG/H.264 校验、HTTP 服务重启续传、分块/文件哈希、伪装格式、并发/重复请求、SQLite v2→v3 迁移/回滚、块/登记/outbox 故障恢复、完成租约接管/取消、真实 Windows junction、配方白名单、非零源起点、多音轨、显式衍生链、源/输出/来源引用、受信登记事务、权限过滤及衍生再上传。新迁移保留版本 1/2 的 SQL 与摘要。原始六模板/表头未改，原件哈希不变。

真实模板验证批次：`jobs/d09-validation-20261107-5370b243/validation.json`。实际 loopback 服务关闭/重建后继续上传，完成后自动入队，两个 CLI work-once 跑通真实 probe/evidence，创建 2 倍速计划；实际文件和 metadata revision 不变。没有渲染、模型、隐私/医学确认，所有媒体/数据库/凭证配置/报告均忽略 Git。项目环境与普通 wheel 安装环境分别全量测试，pip check 通过，继续使用 27 包离线哈希锁。

D17授权文件/预览/下载基础已实现，见对应说明；D04模型及D10打码核心/复核接口已完成工程验证；D11固定变速/组合也已工程验证；下一步D12页面/主动共享及完整语义API。D07 负责常驻部署、容量/保留期、受限账户和恢复演练。D10已有真实打码核心输出，但业务独立集与人员全片复查仍未验收；D11已实现产品变速核心，真实试听/画质及页面验收未完成。
