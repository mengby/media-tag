# 云收件→本地打标→发布回执（M-011）

media-tag 0.2.0 的 `sync` 可选依赖固定 media-contracts==0.2.0/httpx==0.28.1；云端对应media-cloud 0.3.0。共享包以 `config/media-contracts.lock.json` 的wheel/hash为准，不导入兄弟仓源码。

## 独立同步进程

桥接主动请求云端，不要求云端连回个人电脑。桥接独立保存游标、收件待办及发布outbox，通过既有本地HTTP上传/验证/入队；不启动Router、不接管Worker、不使用服务器配置构造本地应用。主DB只读用于校验入库绑定、事务内判重结果、实际模型证据和导出快照。

```powershell
& .\.venv\Scripts\python.exe -m pip install --no-index --no-deps D:/ai/media-cloud/dist/media_contracts-0.2.0-py3-none-any.whl
& .\.venv\Scripts\python.exe -m media_tag sync once --config D:/private/sync.local.json
& .\.venv\Scripts\python.exe -m media_tag sync status --config D:/private/sync.local.json
& .\.venv\Scripts\python.exe -m media_tag sync loop --seconds 60 --config D:/private/sync.local.json
```

`config/cloud-sync.example.json`为无凭证示例。所有路径绝对/本地；sync状态目录不与workspace、对象库或主数据库重叠。cloud使用sync专用token；publisher使用单独publisher token，必须同一origin；每个云actor显式绑定本地稳定actor及其editor/admin HTTP凭证，实际`/ui/session`身份必须相等。source.actor_id/role是本机离线受信管理员导出身份，不作为HTTP请求自报权限。不得把管理员凭证下发给普通上传者。未知人员保持待办，可补映射后重试；已处理actor改绑会拒绝，需要另行迁移，不能按姓名猜测。

## 实际行为与恢复

- 完整收件页先原子落入独立SQLite inbox，再推进cursor；失败事件仍在inbox。feed变化拒绝继续，避免接错云台账。单实例使用OS文件锁，进程退出自动释放。
- 按云upload id派生本地幂等请求；HTTP响应丢失不会重新登记。每次导入验证源hash/大小，续传既有offset；最终解码、入库与派发由本地UploadService完成。completed但未派发、过期validating都会重试幂等complete。
- 主库注册事务内记录new_bytes/duplicate_bytes；同SHA复用物理对象，不合并asset/来源/owner/许可，公开接口不泄漏跨人判重结果。原件只读，入库不写假人工值。
- registered回执只在实际完成入库/获得job后提交。默认required_stages=['ocr']；可配置已有ocr/face/clip/visual_fields，但必须至少一个且全部实际成功。任务未完成、失败、未释放资源、缺少stage、报告/帧损坏都不能报tagged。对不适用或CLIP词表缺失的partial状态保持待办，不能当完整成功。AI初稿不等于完整语义/医学打标。
- 已有Worker执行模型，桥接核对job/owner/source、当前任务及released结果，复验报告/帧hash并记录模型revision与report SHA。默认模型阶段仍ocr/face/clip；`models.stages`仅允许明确选择运行子集，隔离演练选OCR。
- 发布取当前云目录ID集合并本批已打标ID，以本机只读快照生成完整不可变bundle，CAS采用，避免新批覆盖旧库。当前云ID在本地主库不可用时整批失败，不丢弃它继续发布。模型证据改变导致旧proof不在新发布包时回执失败，保留待办供检查，不伪造成功。
- 发布outbox先持久化release/parent/事件，导出失败仅清理此次生成的未完成目录，原件不动；成功bundle保留。push或采用响应丢失后查询云端durable adopted事实；已采用后即使当前版本变化也只补历史回执，不重新激活旧版。竞争CAS冲突会用最新目录重建新的attempt，回退到历史parent也不会撞批次主键。
- ACK提交后丢响应会重放同payload，云端幂等。阶段必须依次推进，published/cloud_active同时绑定同一个实际release及其中模型证据。服务重启不清空队列或回执。status输出pending/errors，业务尚未完成时once仍可成功结束，不要仅以进程退出码判断闭环。

## 运行约束

每次全量发布的成功bundle及下载暂存永久保留，磁盘需求可能按多次全库体积增长；当前没有生产级TTL/磁盘上限/后台清理。非生产联调限制事件/文件数量，监控state_root空间；需要清理时停止桥接，备份sync.db，仅核对status=done对应bundle和phase4下载的生成物，不能删待办/outbox/主库对象。生产自动保留与回收策略未实现，本轮不部署长期服务。

固定原始模板、现有人工值、许可/隐私门槛及本地8765实例未改变。本轮仅新隔离目录与loopback进程验证，未改已部署服务、MCP入口或剪辑来源；未连接真实OSS/ECS、未接企业账号或制作云上传网页。验证证据在control-center/tasks/M-011，云端接口与后补配置见media-cloud `docs/intake-sync.md`。
