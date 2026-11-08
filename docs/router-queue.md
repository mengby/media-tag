# D03 Router 与任务队列

日期：2026-11-07。状态：SQLite 持久化调度、受认证 HTTP 控制接口及本地 CPU 执行适配已实现。使用项目 `.venv`，新增 39 项测试，全量 188 项通过。对应 D03、R04/R10、N03/N04/N05，覆盖 A08 的调度工程前置；D03当次未运行模型/GPU；后续[D04](model-workers.md)已实现本机真实CPU/CUDA模型，生产部署与业务验收仍未完成。

## 状态与事务

D03迁移到版本 2，D09追加[版本3](upload-variants.md)，版本 1/2 的 SQL/摘要保持原样；已有登记、权限、修订在原事务内升级，失败回滚。`router_jobs/tasks/dependencies/attempts/workers/holds/receipts/events/control` 保存 DAG、尝试、租约、持有资源、接收摘要及审计。SQLite 是唯一状态源，任务目录只存私有文件。

阶段状态：`queued → leased → running → succeeded/retry_wait/failed/needs_attention/cancelled`。纯音频抽帧的 `not_applicable` 带真实说明产物，调度依赖视为已完成；`partial` 进入 `needs_attention`，不自动采用部分建议或解锁子阶段。失败分支不阻塞无依赖的独立视频处理分支。

claim 使用 `BEGIN IMMEDIATE` 原子选择、生成 TaskEnvelope、递增 attempt/lease_epoch 和占槽。同一 Worker 同时最多一项未释放的尝试；默认全局 2 个 CPU 槽、1 个 GPU 槽，GPU 任务还占 CPU 槽。所有 GPU 阶段共用同一槽。交互优先为最多 30 秒的有界加权，按等待时间老化，老后台任务可以先于新交互任务。

默认租约 120 秒、心跳 15 秒、每阶段最多 3 次总尝试、单次总执行预算 300 秒；Job 另限总尝试 64 次。服务端可以收紧，阶段请求不能超限。结果即使心跳正常也不能越过总执行时限。瞬时错误指数退避；认证、损坏、未发布词表、结构错误进入人工处理。OOM 只允许由受信阶段适配器提供一次降载配置，仍占总尝试，改变指纹；合成测试仅降低 batch，没有模型切换或覆盖减少。真实模型的降载/覆盖证明留 D04/D05。

租约过期、取消或替换 DAG generation 后，旧结果只记拒收摘要/原因，不采用建议。重复且相同的结果返回幂等成功，内容不同返回冲突；每次接收都有 receipt，主结果及建议只采用一次。采用时重新验证任务绑定、当前权限、源哈希、产物路径/实际 SHA256、证据来源和预算，建议写入与队列确认在同一事务；数据库故障会一起回滚。Worker 原始错误消息不会写运行日志/队列表，保留安全错误码。

结果返回不表示资源已释放。执行器须在处理函数及全部子进程停止后另行 release；失联/超时资源保持隔离，不能仅因旧租约过期再派 GPU。受信 `process_stopped(task_id, epoch)` 探针可证明指定执行已停止，管理员才可回收；默认没有探针时拒绝回收。HTTP 不提供凭一段进程 ID/布尔值强制解锁的接口。真实部署需由服务管理器绑定子进程句柄/执行身份，不能把模型进程仍运行时的心跳丢失当退出。

pause 只停止新派发，已开始任务可以完成；取消通过心跳告知，在安全阶段边界停下，资源仍须真实释放。启动、claim、heartbeat、result 或显式 recover 扫描过期任务；单纯 GET 状态不改状态。没有自动无限重试或隐藏调度线程。

## 复用与增量修订

提交由 owner＋request_id 幂等；同一请求换输入返回冲突。相同输入/版本的未改计划可合并到已有任务，成功结果复用前验证实际产物哈希。已经增量修订的计划不会被当成原计划缓存。缓存损坏会创建新任务，不覆盖旧产物。

`replan` 使用 job revision 乐观锁，只替换指定步骤与它的下游闭包，保留无关成功步骤。旧运行 generation 被 fencing，旧资源隔离后等待真实释放。输入指纹包含源 SHA256、asset/owner、依赖产物摘要、阶段/实现/工具版本、配置、预算、规则/词表/模型版本、提示词版本、目标字段及相关人工值；实现变化须更新受信阶段实现版本。

源文件不变的技术阶段允许基于当前 metadata revision 采用。建议阶段默认拒绝过期版本；显式支持 disjoint rebase 的适配器须证明 revision 历史连续且目标字段未改。人工有效值永远不被建议覆盖。删除/归档、权限撤销、未知修订类型及目标字段冲突均拒绝采用。纯读取阶段只需源 read，向主索引写建议的任务还需 owner 的 edit；Worker 不得到永久素材授权或发布能力。

## 已实现接口

`api.app.create_app(router, credentials)` 中 credentials 为 token SHA256→受信 Principal 的映射。客户端使用原 token 的 `Authorization: Bearer …`；身份和角色由服务端配置绑定，不能从请求体指定。默认无账号/凭证，关闭公开文档和静态文件入口。请求 JSON 严格校验，主体限 1MiB，错误响应不回显原输入/凭证。

| 方法与路径 | 权限/内容 |
| --- | --- |
| `POST /jobs` | editor/admin，`{request_id, spec}`；spec 为 JobSpec |
| `GET /jobs/{job_id}` | owner/admin，仍须 owner 可读源；阶段/尝试/错误码/资源状态 |
| `POST /jobs/{job_id}/pause` | owner/admin，`{paused: true/false}` |
| `POST /jobs/{job_id}/cancel` | owner/admin，协作式取消 |
| `POST /jobs/{job_id}/replan` | owner/admin，`{expected_revision, steps}` |
| `POST /worker/claim` | 已登记启用 Worker；空队列返回 null，不接受调用方指定阶段能力 |
| `POST /worker/heartbeat` | `{task_id, lease_epoch}`，返回 alive/cancel_requested |
| `POST /worker/result` | D00 WorkerResult，仅当前租约可采用 |
| `POST /worker/release` | `{task_id, lease_epoch}`，仅本 Worker 的实际停止确认 |
| `POST /admin/dispatch` | admin，`{paused: true/false}`，全局派发开关 |
| `POST /admin/recover` | admin，扫描过期租约，保留资源隔离 |

401 表示缺少/错误凭证，403 权限不足，404 不可用，409 操作冲突，422 输入错误，413 主体超限。D09已增加[上传和衍生计划路由](upload-variants.md)；D17已增加[授权预览/下载基础路由](file-access.md)，没有审核、发布或语义检索路由，按 D06/D17 后续完成，不能通过静态 jobs 目录替代授权下载。

## 本地运行方式（已实测）

`config/*.local.*` 忽略 Git。`api.config.LocalConfig` 严格读取用户明确选择的文件，不读相邻项目环境。实际目录须预先存在，数据库父目录、对象后端及模板均使用绝对本地路径；不得将来源原件目录用作对象库。新建工作目录需附 README，原始媒体/依赖缓存无需逐层说明。

| LocalConfig 字段 | 内容 |
| --- | --- |
| workspace | 私有工作区，内建 jobs 子目录，不能作为静态目录公开 |
| database / object_store | 本地 SQLite 文件 / 不可变副本目录 |
| template_dir / template_fingerprint | 用户六份原始模板目录及已验证 SHA256，不从示例编造 |
| credentials | 每个独立随机 token 的 SHA256 映射到 `{actor_id, role}`；role 为 editor/worker/admin |
| workers | 已启用的独立 Worker ID 数组，每个须有对应 worker 凭证绑定 |
| options | 可选 RouterOptions，默认值见上述预算 |
| tool_dir | 可选 FFmpeg/ffprobe 目录；省略使用项目固定工具缓存 |

启动命令已实现，运行前必须配置真实本地路径和私有凭证：

```powershell
& .\.venv\Scripts\python.exe -m media_tag serve --config config/router.local.json --port 8000
& .\.venv\Scripts\python.exe -m media_tag work-once --config config/router.local.json --worker-id '配置中的WorkerID'
```

serve 仅绑定 `127.0.0.1`，关闭访问日志；不自动对局域网开放。work-once 最多执行一项已登记阶段，D04可显式启用模型，空队列返回 null；没有常驻轮询、自启动或 Windows 服务安装。配置变化原子更新启用 Worker，启动执行过期扫描，持有资源不被抹掉。

`work-once` / LocalExecutor 是受信服务侧适配器，调用 Router 事务方法，持有本地数据库配置；它不是 OS 权限隔离的模型 Worker。外部 Worker 的 HTTP 协议已验证，独立模型进程、受限账户、文件传输及服务管理器属于 D04/D07/D17。部署时不能给不受信 Worker 主库路径/管理员配置。真实 GPU 执行和进程退出探针仍需相应宿主实测。

内建 probe/evidence 直接复用 D02 的实际工具和限额，调用受租约限制的源读取。输入副本位于 job/inputs，以哈希复用，避免 Windows 深层 CAS 路径；每次仍重新鉴权/验源。新 attempt 原子创建，保存 task/config/技术报告或证据 manifest 和 PNG。D02 工具子进程遇中断也先终止等待，再允许执行器释放资源。心跳独立线程，取消在阶段边界检查，FFmpeg 子操作受自身时间限额约束。

## 实际验证与剩余范围

39 项新增测试包括并发原子 claim、SQLite v1→v2 迁移/回滚、重复/旧结果、文件损坏、事务故障、心跳/总超时、退避/总预算、一次 OOM 降载、CPU/GPU 配额、老化优先级、暂停/取消、增量闭包、人工值/对象权限和 HTTP 角色/输入边界。实际子进程领取模拟 GPU 槽后被终止，Router 重建、受信进程句柄检查、旧结果拒绝及新 epoch 恢复通过；该测试没有 CUDA 运算。HTTPX ASGI 和真实 Uvicorn loopback 均通过。实际生成视频/音频跑 probe→evidence，原文件不变，纯音频正确标为不适用。

真实六模板的私有验证批次为 `jobs/d03-validation-20261107-ddd75bf6/validation.json`：真实 loopback HTTP 提交、两次 CLI work-once、3 张原生 PTS 帧、结果去重、成功复用、输入和 metadata revision 不变。配置摘要、SQLite、媒体和报告均忽略 Git；临时 token 只在验证进程内存，不写入报告。普通 wheel 安装环境另跑全量测试，27 包依赖锁含版本/wheel SHA256，pip check 通过。

D09上传/自动入队和衍生契约已完成，D17文件基础后当前全量277项测试。剩余：D04 真实模型 Worker、D06 审核/导出、D17 文件/API 能力、D07 常驻部署及 GPU/容量/恢复实测。当前没有正式 taxonomy/lexicon 发布，也没有许可或医学确认，不据工程测试签收 A08 整项或其他业务验收。

实现参考：[FastAPI 依赖注入](https://fastapi.tiangolo.com/tutorial/dependencies/)、[FastAPI 测试](https://fastapi.tiangolo.com/tutorial/testing/)。测试实际通过 HTTPX ASGITransport 和真实 loopback 执行，不依赖旧 TestClient 的兼容层。

D12背景分析支持经D17逐对象/隐私链校验的衍生输出；probe/evidence/OCR/face/CLIP领取、心跳及结果采用都会重查来源生产/复核门槛。CPU读取经固定输出哈希验证，不放开D01原件专用入口。人员任务页通过原有replan只重试失败阶段及依赖，见[人员页说明](personnel-ui.md)。
