# CT-22 后端十字段精标初稿投影

本轮接通已合格 precision 报告→初稿检索→原35列CSV；没有新增模型、UI、共享数据库迁移、云发布或生产重跑。正式模型与业务准确率验收仍未完成，不代表 CT-22 全部业务验收通过。

## 复用与新增

复用已有 `precision.projection.verified`，统一重验真实报告及输入字节、源SHA256、任务/模型/预处理/prompt/已发布词表、完整输入证据、字段资格及建议一致性。四字段本地 `DraftIndex._report/_project` 验证规则保持不变；搜索沿用原授权、隐私检查和事务内批量读取。CSV沿用原35列资产表、14列缺口表、人工值、医学复核及原子发布机制。

新增 `media_tag.topic_index.TopicIndex`，将十字段建议放入已有 `ai_draft_documents` 的 precision 槽。该槽仅是可重建投影，不写入 effective_values、不生成确认、不合并素材权限。各字段包含状态、候选和不含私有文件地址的来源摘要，检索原因始终标为 AI 初稿、医学证据未确认。

## 当前范围与失效

按任务登记顺序，对每个字段选取最新 active 范围。该范围尚待执行、失败、取消、未释放、被拒绝或报告被篡改时，不能回退较早 precision 成功，也不能回退本地四字段旧建议。未被新任务覆盖的字段可保留其各自合格范围。unknown、no_match、不适用与失败分别保留，不补正式值。

当前人工有效值永远优先，包括人工明确 unknown 或不适用；当前人工拒绝的建议不再召回。源版本、源哈希、规则/词表、任务生产者、结果释放、取消与人工决定变化会改变投影指纹。每个返回的候选仍调用统一验证函数重验实际报告和输入；当前缓存只允许排除未匹配项。

搜索在已有只读事务中完成媒体验证，不持 SQLite 写锁做媒体哈希。CSV快照也在只读事务中验证建议；发布/下载时额外比较 precision 范围和生产者的元数据指纹，防止新的待办/失败范围出现后继续发布旧字段。该比较只读取元数据，不在写事务中重新哈希媒体。下载仍经过当前权限、源版本及隐私门槛。

`TopicIndex` 是内部投影器，不提供跨账号查询接口。调用者必须先授权每个资产；现有搜索和导出入口会重验权限及 FileService 隐私门槛。公开标签、候选与检索原因不返回原件、报告、适配器或任务产物私有路径，也不返回其他 owner 的判重信息。

## 原表与医学边界

默认导出仍只使用人工有效语义值。`include_ai_drafts=true` 时，非医学六字段可使用当前合格建议：用途大类、画面主体、表现形式、情绪/语气、中文检索词、英文检索词；原表头、枚举及来源/许可规则不变。包含未确认 AI 建议的行仍是 AI初稿，许可不从画面推断。

科室、器官/系统、疾病/症状、过程/状态的 AI 建议仅写入 `quality.json`，保留建议值、证据ID与生产者摘要。四个原CSV医学字段没有人工有效值时保持空白；检索可召回建议，但不会成为已确认医学字段或分类过滤依据。标签确认也不替代入片用途与最终医学审核。

## 验证

使用临时库、合成PNG、真实协议子进程和原始只读模板；未调用实际新模型、下载、外发或生产服务。

已运行以下组合回归，**56 passed，71.09秒**，覆盖原35列、人工值优先、医学侧表、当前范围失效、报告篡改、私有路径、缓存预筛选及只读搜索并发：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/topic_index tests/export tests/search/test_ai_drafts.py tests/search/test_draft_search_speed.py tests/search/test_visual_fields.py::test_visual_fields_reuse_evidence_preserve_human_values_and_enter_search_export tests/search/test_visual_fields.py::test_unknown_output_remains_unknown_instead_of_inventing_values tests/search/test_visual_fields.py::test_unmatched_form_is_distinct_from_missing_information_in_draft_view
```

`git diff --check` 通过；AST 对比确认四字段 `_report/_project` 与原实现一致。正式规模/P95、业务语义/医学准确率和真实模型选择仍待后续验收。

父代理复核追加：精标依赖probe被改时的`ModelFailure`现在归为当前建议不可用，检索撤下候选，不向外抛处理异常；原技术CSV门槛仍拒绝损坏probe。已新增实际探测报告篡改回归。

父代理最终联合回归：`tests/similarity tests/analysis tests/upload/test_analysis_repair.py tests/topic_index tests/export tests/search/test_ai_drafts.py tests/search/test_draft_search_speed.py` **157项通过（95.15秒）**。此前178项关联回归与本批重叠，不相加；私有证据`jobs/ct-similarity-topic-index-20261111-892e6c41/validation.json`，六份原模板及旧契约/依赖锁摘要保持。
