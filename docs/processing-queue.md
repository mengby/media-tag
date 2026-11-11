# 清洗及图像遮罩队列接线

2026-11-11：本仓库可调用的本地工程接口、HTTP 与配置执行器接线；未启用生产实例，未接人员页。

## 已实现接口

`media_tag.processing_queue` 提供两个 CPU Router 阶段：`cleaning_output`、
`image_mask_output`。阶段输入是严格 Pydantic 契约，禁止额外字段及客户端任意路径。
请求仅接受当前 Catalog 的素材 ID、预期 revision、源 SHA256、幂等 request_id 和明确配方。
部署配置默认 `enabled=False`，CPU、输入/输出和运行时间预算由服务器设置。

```python
from media_tag.processing_queue import (
    CleaningSubmission, ImageSubmission, ProcessingDeployment,
    ProcessingHandlers, ProcessingService, stages,
)
from media_tag.cleaning import CleaningRecipe
from media_tag.cleaning.models import VideoSegment
from media_tag.router.runtime import LocalExecutor

# router、tools、worker、actor 为当前受信任应用构造的对象。
options = ProcessingDeployment(enabled=True, timeout_seconds=300)
router.stages.update(stages(tools))
router.register_worker(worker.actor_id, tuple(stages(tools)), principal=admin)
service = ProcessingService(router, tools, options=options)
handler = ProcessingHandlers(router, worker, tools, options=options)
executor = LocalExecutor(router, worker, handler)

request = CleaningSubmission(
    request_id="clip-request-1", asset_id=asset_id,
    expected_revision=current_revision, source_sha256=current_sha256,
    recipe=CleaningRecipe(
        source_sha256=current_sha256, content_kind="video",
        segment=VideoSegment(start_seconds=2, end_seconds=6),
        target_duration_seconds=4, audio_policy="remove",
    ),
)
accepted = service.request(request, principal=actor)
executor.run_once()
status = service.status(accepted["job_id"], principal=actor)
```

以上代码调用已实现接口；变量由宿主提供，不能直接当部署脚本执行。现有多阶段执行器
应在其 handler 分派中将这两个阶段路由至 `ProcessingHandlers`，其余阶段保持原有 handler。
图像请求使用 `ImageSubmission` 和现有 `ImageRecipe`：明确 `png/apng`、遮罩目的、方法、
显示坐标及覆盖帧。没有自动人脸检测或自动水印定位，不实现内容补全式去水印。

## HTTP 与 LocalConfig

`LocalConfig.processing` 使用同一严格 `ProcessingDeployment`，默认关闭。启用后
`load_application()` 注册两个 CPU 阶段和请求服务，`executor_from_config()` 注册 worker
能力并分派 `ProcessingHandlers`，不要求 semantic/local_topics/precision 启用或成功。
下面是配置片段，不能替代包含当前受信任身份和绝对本地路径的完整配置：

```json
{
  "processing": {
    "enabled": true,
    "timeout_seconds": 300
  }
}
```

已实现端点：

| 请求 | 行为 |
| --- | --- |
| `POST /processing/cleaning` | 完整 `CleaningSubmission` JSON，受理标准副本/明确视频配方 |
| `POST /processing/images` | 完整 `ImageSubmission` JSON，受理明确图片/动画遮罩配方 |
| `GET /processing/jobs/{job_id}` | 校验当前授权和实际产物后返回状态，不返回路径或字节 |
| `POST /jobs/{job_id}/cancel` | 复用已有 Router 取消入口及相同身份检查 |

全部沿用现有 Bearer/本机 UI 会话认证；不接受 body 内的 principal、owner、路径或服务器
限额。默认流式请求上限 1 MiB（沿用 `create_app.max_body_bytes`），超限 413；严格契约
失败 422，未认证 401、无编辑权限 403、CAS 冲突 409。配置关闭时认证后的 processing
端点返回 503 `processing_disabled`，阶段不注册。请求与状态的同步服务调用均在
threadpool 执行，响应包括 `Cache-Control: private, no-store`。不存在处理输出下载端点。

## 权限、源绑定及生命周期

请求人必须为 editor/admin，且其 actor ID 当前拥有素材对象的 edit 权限；本接线不凭
stage config 中自报的管理员身份获得跨对象权限。管理员处理他人素材也需显式对象 edit
授权。Router 持久化 job 的 actor ID 与 config 中的请求人一致，worker 不冒充源所有者。

请求前检查对象权限、源 SHA256、revision 和 FileService 当前源门槛。Router 的提交事务
独立，因此提交后再做 CAS；发生竞争就取消该任务，不返回“处理成功”。worker 领取后、
源读取及采用结果前重复检查当前 ACL、revision、所有者、有效 lease 和真实输入哈希。
请求后授权撤销、源修改或待隐私复核衍生不能绕过这些门槛。

原件通过受授权的 `Router.open_input` 只读复制到私有、绑定 task/epoch 的工作空间，实际
SHA256 验证后才交给清洗/图像核心。Windows Job Object 管理原生子进程及 FFmpeg，取消/
超时后先停止进程树，再由原执行器释放资源。编码维持核心的一线程预算，不调用网络或
模型。输入、输出、日志、响应大小及时间均有界限。

清洗核心实际输出标准副本，视频实际裁剪/变速；默认 1.0，不自动将长视频压为 3～5 秒，
不裁 BGM 或 GIF 循环。显式片段与目标时长、音轨策略等沿用
[cleaning-core.md](cleaning-core.md)。源 SAR、音轨或证据未满足明确配方时保留
`waiting_policy/waiting_evidence`，不会写成成功。图像帧覆盖不足、未知零延时/颜色策略也
保留等待状态。

`ProcessingService.request()` 仅返回受理结果；`processing_completed=False`。
阶段返回成功仅表示工程产物实际存在且通过完整性检查。只有当前成功 attempt 已释放、
源与报告/产物哈希仍有效时，`status()` 才返回 `pending_registration_review`。等待策略和
证据返回具体 issue_code，Router 为 `needs_attention`；失败不附带可采用输出。
取消、源变化或证据损坏不会回退上次结果。状态响应仅返回哈希、大小与安全摘要，不返回
私有路径或下载地址。

## 产物及审核边界

`workspace/processing-work/<task-epoch-hash>/` 存私有输入快照和核心缓存；
Router 的 `task.output_prefix` 存真实输出、核心报告、`processing.json` 及原生日志。
报告绑定 task/job/lease、asset/revision、源 SHA256、输入 fingerprint、配方 SHA256、
核心版本、结果哈希/大小。配对核心报告保留变换、时间映射和具体检查依据。

此接线不写 Catalog 原件或 variant，不自动导入、不写有效标签、不发布或开放下载。
所有输出明确 `asset_registered=False`、`registration_status=pending_registration`、
`review_status=pending`；privacy/quality/medical confirmation 固定为 False，许可未核实。
图像已执行遮罩并不等于隐私复核通过。产物不能拿原素材 ID 与新输出哈希从 FileService
下载；其哈希必须与当前登记内容相符。

后续共享接线尚需：衍生契约扩展（标准副本/裁剪/图像遮罩）、
绑定真实输出哈希的登记与隐私/质量复核、受审核门槛约束的预览下载及人员页。
这些步骤未在本模块完成，不能把队列工程成功当作整个在线处理或 CT 业务验收。

## 实际验证

隔离合成素材测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/processing_queue -q
```

覆盖标准副本、真实红/蓝视频裁剪、RGBA 遮罩、GIF→APNG 帧和时长、音轨策略等待、
逐帧人脸遮罩覆盖等待、编辑权限/授权撤销、前后 CAS 竞争、源哈希损坏、产物篡改、
未释放 attempt、取消进程树、报告审批字段不可伪造，以及现有待隐私审核衍生的源门槛。
测试使用临时目录/数据库，未打开生产实例、改部署配置或下载模型。

本模块实际结果为 18 项通过（11.34 秒）；包括 Job Object 分配失败时先终止并等待尚未
开闸的 Python 子进程，防止退出失败分支后遗留未纳管进程。

另有 4 项隔离 `LocalConfig → HTTP → executor` 回归实际执行标准副本、RGBA 遮罩及视频
裁剪，验证认证、禁用配置、CAS、无任意路径、请求体预算、threadpool、取消、无原件登记
和无下载。仅模板加载边界使用合成三字段契约，未读取私有原模板；本地配置、数据库、
对象及进程执行均为真实隔离实例，不使用生产数据或模型。

## 采用前有界验证与最终授权

公开 `verified_result(router, tools, row, asset)` 返回
`(task, result, report_or_none, report_ref_or_none)`。调用方须提供当前授权的源素材行和
Router task/attempt 行；只执行工程产物验证，不代表登记或审核许可。验证先限定精确的
artifact 种类、attempt 前缀及所有文件实际大小，再读取报告：报告/核心报告各不超过
16 MiB，媒体不超过该任务服务器配方限额，且实际大小必须等于报告 output_bytes。
随后校验源、配方、版本、核心报告、摘要、结果哈希及固定“未批准”字段，按大小有界读取。
此路径不先调用通用无界 artifact hash。失败结果没有工程产物时 report/ref 可为 None。

长时间字节验证应在写事务外执行。完成后调用方必须开启新事务，重新检查当前 actor
对象 edit 权限、FileService 门槛、源 hash/revision/owner，并调用
`require_current_attempt(connection, row)` 重验当前 active/state/lease_epoch、spec、
envelope、result、released 和 job cancellation。状态服务已执行这些检查；读字节期间
撤权、取消或源/attempt 改变不会返回处理完成或输出元数据。

原生子进程仅继承 Windows/Python 启动必要环境 allowlist 和显式 CPU/离线标志，不继承
API/OSS/云凭证；2 MiB 合计日志预算在循环及子进程退出后均检查。新增 11 项回归使用
真实超限文件并禁止 read、状态 hash 期间撤权/取消/源与 attempt 变更、真实快速退出的
2 MiB+1 日志子进程和敏感环境变量不继承；全部通过，未调用模型或生产实例。

父代理最终回归：`tests/processing_queue tests/router tests/similarity/test_config.py tests/precision/test_runtime.py::test_configured_worker_dispatches_and_executes_selected_bridge tests/local_topics/test_analysis_pipeline.py::test_configured_worker_adopts_unknown_image_without_loading_weights` **75项通过（39.41秒）**。全部为隔离合成处理/协议，无真实模型或生产操作。私有证据`jobs/ct-processing-queue-20261111-80ac6d14/validation.json`。
