# 分析阶段复用与补缺修复

2026-11-11。CT-05部分实现，已在隔离实例验证；**尚未完成跨来源/账号的字段级语义复用、人工拒绝后的补标及CT-05整体验收**。本轮复用的是本请求绑定作业中兼容且证据有效的阶段，不复制旧租约、人工值或批准，也不启动新模型。

## 明确触发的修复入口

已实现`POST /analysis/assets/{asset_id}/repair`。使用现有认证、请求体大小限制和严格JSON校验；只有当前请求人有edit权、自己的意图已绑定当前作业、文件门槛放行且analysis.enabled显式启用时才能新建修复。默认关闭，生产配置未变。GET状态接口不触发修复。

请求包括request_id、expected_asset_revision、source_sha256、expected_job_revision和可选fields。修复前检查实际asset/owner/源哈希、对象授权、隐私/生产者门槛、作业归属及版本。暂停或取消的作业不自动恢复。已接受请求重试也重新检查当前权限和文件门槛。

fields只能选择原十字段；空列表表示检查全部已配置阶段，不表示十字段能力已齐。指定字段映射到能够产出该字段的已配置阶段及其祖先；没有能力的字段进入pending_capability_fields。纯音频两项视觉字段标为not_applicable_fields，其余缺能力不能冒充不适用。当前四字段Worker共享一个阶段，修复其中一项仍需按该阶段整体执行；字段级成本优化与精标请求选择尚待后续。

## 保留与重排规则

| 当前阶段 | 处理 |
| --- | --- |
| 规格/版本兼容、成功且已释放、实际输出校验通过 | valid，保留原task_id和已成功尝试 |
| queued/leased/running/retry_wait或结果尚未释放 | pending，不独立重启该阶段 |
| 已配置但尚缺该阶段 | missing，补入原作业 |
| 规格/模型/预处理/规则配置不兼容 | incompatible，按当前已配置规格重排 |
| 失败或实际报告/帧缺失、哈希不符、来源不符 | failed/unavailable，重排失效根阶段及其依赖 |
| CLIP仅因未发布词表而partial，实际特征仍校验有效 | awaiting_vocabulary，保留特征，不无效重复推理 |

复用资格按当前意图的同一实际文件、阶段规格/版本、当前尝试及实际artifact校验判断；视觉模型报告额外复用原报告/帧/建议绑定检查，visual_fields不能用not_applicable掩盖缺少视觉初稿。没有从文件名或OCR补造语义。

依赖根失效时，下游即使自身输出仍完好，也须由Router按DAG重排；这包括已经启动的下游，原租约/资源仍按原取消与隔离规则释放，不能复制旧成功冒充新执行。未受影响的有效阶段保持，执行仍受原作业的总尝试预算限制。配置变更只在明确修复时采用，不在查询时暗中升级。

响应区分stage_assessments、patched_roots、实际replanned_keys、未受影响reused_stage_keys以及缺能力字段。accepted_job_revision是接受修复时的快照，后续实际运行状态仍从GET接口读取。analysis_complete、medical_confirmation、effective_values_written保持false，阶段valid不等于语义准确或医学确认。

## 原子回执和并发边界

先在只读事务中核验证据，不持有SQLite写锁进行报告/帧哈希读取。写事务再检查当前权限、来源、作业版本、任务generation/状态/lease_epoch；并发变化则拒绝并要求重新读取状态。

Router新增内部`_replan_tx`事务接口，原replan对外行为保持。重排与analysis_repair审计回执在同一事务中写入；回执失败时，任务代次和作业revision一起回滚。回执使用原Router事件表，不新增数据库迁移或执行队列。

request_id按当前actor和绑定job限定，指纹包含asset与完整修复请求。同一请求确认丢失后可重试，返回原接受结果及reused_request=true，不重复重排或消耗代次；复用同一ID但改输入会冲突。回执不授予权限，不表示之后已执行成功。

## 验证与剩余工作

隔离测试覆盖实际合成图片CPU probe/evidence、有效阶段保留、损坏probe/evidence依赖树修复、预处理规格变化、请求重开幂等、回执写入失败原子回滚、已有待执行阶段不重启、合成能力声明补缺、HTTP认证/CAS/请求校验、隐私门槛，以及CLIP未发布词表不重跑。合成能力声明只证明派发协议，不证明模型推理。

命令和结果见[执行记录](cleaning-and-topic-tagging-execution.md)。后续仍须实现跨来源/账号的合格字段复用、片段/时间范围及词表版本资格、人工拒绝补标和更多模态；不得把本轮阶段补缺计作全量字段复用或完整精标接入。
