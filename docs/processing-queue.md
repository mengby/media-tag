# 本地清洗、图像处理与派生登记

本仓库已实现严格处理请求、两个 CPU 阶段、真实工程输出、版本化派生登记、独立复核及
授权文件入口。默认关闭；本轮仅使用隔离合成实例，未打开或迁移生产库、修改生产配置、
重启服务、下载模型或操作相邻仓库。真实审核人与业务验收仍待提供。

## 配置与接口

LocalConfig.processing 使用严格 ProcessingDeployment，enabled=False、auto_adopt=False，
quality/watermark/privacy 指定审核人均默认空。启用后注册 cleaning_output、
image_mask_output 两个 CPU 阶段，由配置执行器分派 ProcessingHandlers，不依赖语义、
本地十字段或精标成功。以下只是启用时的配置示例，本轮没有写入实际部署配置：

```json
{"processing": {"enabled": true, "auto_adopt": true, "timeout_seconds": 300,
 "reviewers": {"quality": [], "watermark": [], "privacy": []}}}
```

请求只接受当前 Catalog 的 asset_id、expected_revision、source_sha256、request_id 与
明确 recipe，禁止客户端路径、principal、owner、限额和额外字段。沿用现有 Bearer/本机
UI 会话身份；actor 必须有当前对象 edit 权限。管理员也不能凭 stage config 自报角色
绕过对象 ACL。默认流式请求上限 1 MiB，服务同步工作使用 threadpool。

| 已实现请求 | 行为 |
| --- | --- |
| POST /processing/cleaning | 严格 CleaningSubmission，标准副本或明确视频配方 |
| POST /processing/images | 严格 ImageSubmission，图像/动画遮罩配方 |
| GET /processing/jobs/{job_id} | 当前工程、登记与有限失败重试状态，无私有路径 |
| POST /jobs/{job_id}/cancel | 复用 Router 取消 |
| POST /processing/jobs/{job_id}/adopt | 验证并登记当前成功且已释放的工程产物 |
| GET /processing/outputs/{asset_id} | 派生身份、哈希和三个独立复核状态 |
| GET /processing/outputs/{asset_id}/comparison | 授权私有原件/结果对照、配方和时间映射 |
| GET/HEAD /processing/outputs/{asset_id}/preview-content | 私有结果字节，固定实际输出 SHA256 |
| GET/HEAD /processing/outputs/{asset_id}/source-content | 独立授权原件对照，不是失败回退 |
| POST /processing/outputs/{asset_id}/reviews | 严格 OutputReview，按维度批准或撤销 |

未认证 401、无权限 403、CAS 冲突 409、契约失败 422、请求超限 413。关闭时 processing
端点认证后返回 503 processing_disabled，两个阶段不注册。响应 private/no-store，没有
按任务私有路径直接下载的入口。

Python 接口为 ProcessingService(router, tools, options=...).request()/status()；
ProcessingDerivedService 提供 adopt()/status()/comparison()/review_files()/open_source()/
review()/adopt_ready()。stages(tools) 注册阶段，ProcessingHandlers 是原执行器 handler。
auto_adopt 开启后，领取前与释放后自动采用；仅使用服务端实际 credentials 中的
editor/admin actor，不从数据库 owner_id 编造管理员身份。

## 真实处理和证据

清洗默认速度 1.0。明确源片段才执行裁剪/变速，不自动把长视频压为 3～5 秒，不裁 BGM
或 GIF 循环。音轨、非方形像素、源时钟等未选策略保持 waiting_policy/waiting_evidence；
实际 FFmpeg 裁剪和源/结果映射见 [cleaning-core.md](cleaning-core.md)。图像遮罩使用实际
显示坐标、保护 alpha，动画明确 APNG 并保留逐帧时长；不自动检测人脸、定位水印或补全
水印背后内容。

原件只读；worker 经 Router.open_input 取得当前授权源，复制私有快照并核对实际 SHA。
Windows Job Object 管理 Python/FFmpeg，取消/超时先停止进程树再释放资源，编码维持一
线程预算。子进程仅继承必要环境 allowlist 和显式 CPU/离线标志，不继承 API/云 token。
合计 2 MiB 日志预算在循环及退出后检查。

processing-work/<task-epoch-hash> 保存私有输入和核心缓存；Router attempt 保存实际
媒体、核心报告、processing.json 及原生日志。报告绑定 task/lease、源 asset/revision/
SHA、input fingerprint、recipe SHA、核心版本和实际输出 hash/bytes。阶段成功仅工程
产物，状态 pending_registration_review，不自行批准隐私或质量。

公开 verified_result(router, tools, row, asset) 返回
(task, result, report_or_none, report_ref_or_none)。先限定精确 artifact 种类、attempt
前缀和所有实际大小，再解析绑定；报告/核心各最多 16 MiB，媒体受服务器配方预算约束且
等于 output_bytes，随后有界哈希，不先通用无界扫描。长 IO 不持 writer；结束后新事务
重验 actor ACL/FileGate/source CAS，并 require_current_attempt(connection,row) 重验
active/state/epoch/spec/envelope/result/released/cancel。撤权或取消不宣告处理完成。

## 派生登记与 migration 12

登记先在 writer 外有界验证证据、实际探测/完整解码及不可变对象 ingest。视频/音频复用
现有 probe/full decode；图片/APNG 复用 Pillow 全帧解码，记录真实 kind、透明、帧 RGBA
哈希及精确分数时长，不放宽旧 FFmpeg 白名单或把 APNG 冒充静态。随后短事务重验源
actor/ACL/FileGate/revision/owner 和当前成功已释放 attempt，原子写 derived、variant
plan、variant、source links 和 ensure_intent。提交失败不留下假登记；未引用中间对象不
成为可下载素材。

variant_plans.spec_json 使用 ct-processing-variant-v1，不伪装旧视频 Recipe/PlanSpec。
契约绑定真实 recipe、源、producer、输出及核心变换证据。视频保存真实裁剪/倍速映射与
累计倍速；图像/动画/音频明确视频时间映射不适用，不伪造 FixedTimeMap。相同 blob 仍
有独立 derived 身份和来源链，不继承人工值、人员或许可。相同 task/epoch 采用幂等。

仅追加 migration 12，1～11 语句和 checksum 不变：

- processing_derivations 关联 variants，固定契约/摘要及复核需求。
- processing_output_reviews 保存 quality/watermark 独立不可变事件，绑定 output SHA、
  recipe hash、contract version、actor、asset revision；privacy 复用 privacy_reviews。
- processing_adoption_receipts 保存实际登记、有限安全错误码及 2～300 秒失败退避，
  未尝试/最早尝试优先。八个失败不饿死后续结果，重启可重试，成功身份不重复。

备份/还原兼容 9/10/11/12，隔离还原再打开数据库会升级 12。旧只读非 processing variant
不依赖新表；新 processing 契约缺失配对记录时失败关闭。

## 复核和统一文件门槛

所有结果要求 quality；明确 watermark 遮罩另需 watermark。视觉结果保守要求 privacy。
标准复制或隐私通过本身不证明人脸打码。纯音频“真人脸复核”不适用，
audio_voice_privacy_reviewed=False，不代表声源隐私、版权或医学批准。

私有对照允许当前所有者或指定审核人，并要求当前输出及源权限。批准者必须在该维度的
真实指定配置中、有输出 edit 和源 read 权限，提交确切 SHA、recipe hash、contract
version、expected_revision，以及完整查看/源对照/维度检查。服务器重新验证源与结果
实际字节，最后事务再检查角色/ACL/producer/祖先门槛。指定身份不自动获得对象权限。
撤销只降低访问，不要求先成功打开损坏的原件或结果。

FileService._gate 沿来源链校验 quality/watermark/privacy/producer。私有复核仅跳当前
结果的待复核，祖先不跳。全部通过后复用固定 SHA 的 /v1/assets/ 预览/下载；撤销立即
阻断同一入口，不回退原件。现有普通检索、导出、发布已调用 FileService，无新增旁路。

review/export 的“含人脸=已处理副本”使用 has_reviewed_face_processing：新处理必须有
登记时从实际核心报告提取的 face region/frame 证据；旧视频需真实 mask 和 transform
producer/report 契约。允许有效祖先已打码而当前只复制/裁剪/变速，但整链仍须可用且
复核通过。不从人工标签、同 SHA 或单独隐私批准推断打码。

任务状态中的固定确认字段属于工程阶段；人工结果以派生输出 reviews 为准。人员操作页、
真实审核人配置、生产启用及真实素材业务签收尚未完成。标签确认、医学审核和当前剪辑
用途检查继续独立。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest tests/processing_queue -q
.venv\Scripts\python.exe -m pytest tests/maintenance/test_backup.py tests/router/test_recovery.py -q
.venv\Scripts\python.exe -m pytest tests/export/test_processing_privacy.py -q
```

测试使用隔离目录/SQLite、真实 CPU worker/HTTP/native 子进程及合成媒体。HTTP/config
仅在模板加载边界使用合成契约。覆盖真实副本/RGBA/APNG/视频裁剪/音频、源和 producer
竞争回滚、权限撤销、各维度复核/撤销、稳定入口、进程/IO 预算、重启登记、1～11 摘要
保持及旧/新备份还原。工程通过不替代标签准确性、许可和医学/隐私业务验收。
