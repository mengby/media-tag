# CT-21 十字段人工复核资格

本轮复用现有 ReviewService 和 HTTP，不新增模型、人员、批准身份、UI 或接口。适用于已登记的十字段 precision/local_topics 建议以及既有建议。没有开展业务医学签收或模型准确率验收。

## 当前报告与字段范围

人工采用前核对建议台账与当前 task/attempt 完整身份、源 SHA256、实际释放状态、任务启用及取消/暂停状态。precision 报告复用 `precision.projection.verified`；local_topics 报告复用 `local_topics.projection.verified`。两者原有源、证据、词表、prompt、模型及字段资格校验没有放宽。local_topics 的生产校验仍拒绝 synthetic_protocol 结果。

同一素材同一字段只允许最新 active 任务范围的建议参与复核，通过 `analysis.scopes.latest_topic_fields` 复用 precision、local_topics、visual_fields 的统一范围。没有生产 topic 范围的旧自定义阶段保留当前精确字段选择。新范围待执行、失败、取消、未释放或属于另一模型时，不会回退旧成功。被取消/暂停的整个任务组也不允许继续采用其其他历史建议。旧四字段的报告验证规则保持原状，新增当前范围资格在其外层生效。

人工已有有效值（包括 unknown、不适用）或已拒绝同一建议时，不允许用 accept 隐式覆盖。需要修改时仍可通过现有明确 manual 决定，并满足原有证据、权限和字段约束。AI 重跑本身不改人工有效值。重复同一合法人工请求仍按原幂等记录返回。

## 现有 HTTP

`GET /review/assets/{asset_id}` 继续列出十字段建议与历史。`available` 表示当前生产报告资格；`accept_eligible` 额外考虑人工优先和实际医学审核人配置；`review_state` 区分等待人工、已有人工有效值、人工拒绝、缺少医学审核人及生产报告不可用。当前报告可用也不代表已经观察素材或通过医学审核。

`POST /review/assets/{asset_id}/fields` 继续按确切 run_id、suggestion_id 和字段接受、拒绝或人工修改。接受/拒绝先在只读事务验证实际报告字节，再在写事务重验权限、素材版本及生产者/人工状态元数据栅栏，最后原子写入；生产者在验证期间变化时拒绝保存。

`GET /review/assets/{asset_id}/evidence/{run_id}/{evidence_id}` 仍经当前资产编辑权限、建议资格及 FileService 门槛，并在实际字节读取后重验生产者/人工状态元数据。某个 run 的首字段已被新范围覆盖时，其余仍合格字段可继续取得自身绑定证据；不会回退不合格建议或未授权原件。原有单次证据响应32MiB限制保持，较大完整素材需使用已有授权素材入口观察。同一详情请求的十字段共享报告仅验证一次实际字节，每个字段仍分别检查当前范围和人工状态。

## 人工与隐私边界

科室、器官/系统、疾病/症状、过程/状态仍须明确配置的医学审核人逐字段决定。所有非拒绝决定仍要求明确人工观察记录；医学实际值要求当前人员实际观看/试听对应源版本。动态方向仍要求连续视频观察，静态帧不能满足该条件。

接受 AI 建议时，连续观察要求还包含实际通过共享验证的生产者 `FieldAssessment.dynamic`，即使值没有箭头（如“收缩”），客户端传 `dynamic=false` 也不能降低该要求。只读报告验证同时取得该字段声明的真实video_segment范围；当前审核人必须实际连续观察覆盖每个范围，可以合并相接或重叠片段，但不能跨空洞，也不能借用别人的观看记录。precision目前声明完整输入范围，因此仅观察其中很短片段不够；不猜测模型事件发生的细时间。写事务复用该只读结果并重验绑定报告哈希的元数据栅栏，不重复读取大文件。详情给出 `producer_dynamic`、`producer_dynamic_ranges`、`requires_continuous_observation`；人工决定记录实际要求范围与绑定摘要。明确 manual 输入仍按原人工规则，不从词义猜测动态。

已验证报告中“过程/状态”的明确箭头方向也沿用同一声明范围覆盖规则，与AnalysisDocument既有动态约束一致；不需要客户端再声明dynamic，也不从普通词义推断运动。

来源、许可、人脸及整行确认继续使用原专门规则。标签人工确认不会批准人脸输出、解除隐私门槛、证明许可或批准剪辑用途。

## 验证边界

验证使用临时库、合成图像、真实 precision 协议子进程及隔离 local_topics 协议夹具；没有下载或运行实际新模型。local_topics 夹具只模拟预期协议，另测一致改写哈希后的 synthetic_protocol 报告仍被生产验证拒绝。

包含生产者动态范围覆盖修复的组合回归 **103 passed，111.85秒**：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/review tests/local_topics/test_router.py tests/export tests/topic_index tests/search/test_ai_drafts.py tests/search/test_visual_fields.py::test_visual_fields_reuse_evidence_preserve_human_values_and_enter_search_export
```

动态回归使用实际发布词项、真实两帧GIF、真实precision协议子进程与共享报告验证：image观察、极短片段、缺失开头、内部空洞和借用其他人员观看均被拒绝；相接/重叠实际片段可覆盖声明范围；客户端false不能降级，明确manual输入继续原规则。增加明确箭头声明的相同范围覆盖后，对最终 `tests/review` 重跑 **48 passed，49.98秒**。`git diff --check` 通过。业务样本和指定人员最终验收仍待进行。

父代理复核后最终回归：`tests/review tests/local_topics/test_analysis_pipeline.py tests/topic_index tests/export` **81项通过（89.73秒）**，包含最新动态范围规则与本地/精标主流程兼容。此前103项关联与48项review批次有重叠，不相加。私有证据`jobs/ct-topic-review-20261111-478abf32/validation.json`。
