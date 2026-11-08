# D16 术语评测工具与验收边界

2026-11-08：已实现独立数据契约、分组防泄漏、原文/纯词表/词表+BGE 对照、带分母的指标、离线阈值校准和私有报告。实现位于 `src/media_tag/terminology/evaluation/`、`scripts/evaluate_terminology.py`。使用项目 `.venv`，无新增依赖。**正式业务评测及指定 Windows 负载验收未完成**；用户已暂缓提供正式词表、代表性素材/独立标注集、实际角色和目标机器/宿主资料。工具验证不替代这些输入。

## 冻结输入

先启动已配置 D12/D15 的实际服务，以有明确对象 read 授权的人员凭证冻结候选范围。凭证只放忽略的本地文本文件，命令行不携带原 token；远端要求 HTTPS，本机允许 loopback HTTP，拒绝转发和带用户名/密码的 URL，不使用系统代理。

```powershell
.venv\Scripts\python.exe scripts/evaluate_terminology.py --snapshot --base-url http://127.0.0.1:8000 --token-file config/evaluation-token.local.txt
.venv\Scripts\python.exe scripts/evaluate_terminology.py --schema
.venv\Scripts\python.exe scripts/evaluate_terminology.py --check-dataset --dataset data/evaluation/evaluation.json --calibration data/evaluation/calibration.json
```

第一条生成忽略的 `jobs/d16-scope-*/scope.json`，含当前查询整组 pin、当前账号可用素材的稳定 ID/修订/SHA256 和候选池摘要，**不生成查询答案或人员确认**。第二条输出严格 JSON Schema，便于按真实来源准备独立标注。不要把 `.local.*` 凭证文件或真实评测集提交 Git。

| 数据字段 | 要求 |
| --- | --- |
| schema_version / dataset_id / split | d16-evaluation-v1；标识；calibration 或 evaluation |
| purpose | synthetic_engineering 或 business，合成结果不能进入业务签收 |
| frozen_at / annotators / adjudicator / annotation_reference | 含时区冻结时间、实际标注/裁定身份和可追溯依据；工具不能验证身份真实性，须人工核实 |
| independently_human_annotated | business 必须显式 true；不能以待测模型自己生成答案再自行验收 |
| pin / candidate_pool_sha256 | taxonomy/lexicon/index/model/preprocess/match 固定版本、阈值/超时/缓存策略摘要及账号范围摘要，与 scope 文件一致 |
| calibration_sha256 | evaluation 必须绑定独立校准文件的字节 SHA256 |
| criteria | 事前冻结门槛/四类别数量；默认是专题拟定基线，缺 approved_by/approval_reference 不表示批准 |
| cases | 每条 case_id、source_group、query/context、category、expectation、概念集合、允许自动扩展集合、实际相关素材和依据 |

case 的 category 为 common/ambiguous/challenge/out_of_scope；expectation 为 clear/clarify/reject，常用表达必须明确、多义必须澄清、库外必须拒识。明确表达需给出非空 acceptable_concepts 和允许自动扩展子集。其他表达不能允许自动扩展。可指定 forbidden_concepts、forbidden_asset_ids、expected_polarity、expected_direction，逐例捕捉错误疾病扩展和反方向命中。只有人工已经穷尽固定候选库的相关集合，才能置 complete_relevant_set=true。

拒绝重复 ID、归一化后重复查询、相互矛盾的标注、重复 JSON 键和非有限数值。文件最多8MiB、10000条；相同来源/近义改写必须归到同一 source_group。校准与验收的来源组和规范化查询不能重叠，版本及候选池必须一致。工具只能检查所填分组，实际语料来源与独立性仍须由人员核实。

## 离线校准

校准只读取 calibration 集和事前冻结的 D14 索引。索引目录含 documents.json、index.json、vectors.f32；校验文件为这三个固定文件名到 SHA256 的 JSON 映射，必须由已审核索引记录取得并事先冻结，不能测完再改摘要。校准验证文件哈希、版本、512维、行数及归一化，使用锁定 BGE 本地权重实际编码，不联网下载。

```powershell
.venv\Scripts\python.exe scripts/evaluate_terminology.py --dataset data/evaluation/calibration.json --calibrate-index jobs/ACTUAL_INDEX_DIRECTORY --bundle-file data/evaluation/index-checksums.json --model-root cache/models/d14
```

上述目录是填写实际路径的操作示例，不是本机已有正式业务索引。输出0.50～0.95候选阈值网格、top1候选精度、覆盖、Recall@5及库外误召回；有推理失败、库外误召回或无符合条件阈值时不推荐。另列 margin 歧义提示对照，**margin 不授权模型自动确定医学概念**。最多500条、查询数×概念数不超过100万；保存前5候选和故障，不保存所有密集得分矩阵。

该步骤只测 BGE 组件，不声称测了词表路由或完整检索，不读取验收集、不自动改线上阈值/发布词表。建议值必须在独立验收中复测；本次两条合成校准得出的数值没有应用到服务默认参数。

## 对照与指标

```powershell
.venv\Scripts\python.exe scripts/evaluate_terminology.py --dataset data/evaluation/evaluation.json --calibration data/evaluation/calibration.json --base-url http://127.0.0.1:8000 --token-file config/evaluation-token.local.txt
```

同一账号、同一查询与语境，交替运行 literal、lexicon、lexicon_bge 三路 `/search/query`，关闭未确认 AI 初稿。BGE 候选保持待选择；不使用正确答案替用户选择，不把这种“答案选择”当真实检索收益。每次响应校验固定整组，开始和结束核对授权素材池；版本/可见范围变化使整个报告失败，不混用结果。素材排名按画廊默认版本取每根最后一个版本，前10根各取一个固定版本，标注相关集合也须按这个排名单位准备。

- 明确解析精度：全部正确明确解析 / 全部明确解析；分母0为 null，不通过。常用表达覆盖独立统计。
- 概念 Recall@5：对有正确概念的查询，逐条覆盖比例再宏平均。失败项计0，不从分母删掉。
- 素材 Hit@10：有至少一个相关命中的查询 / 存在可见相关素材的查询；没有相关素材的数量单列。完整相关集合另外报告宏平均 Recall@10，不能混称。
- 原文路线不提供术语精度/拒识指标；两条语义路线另报拒识、歧义、期望状态一致率、错误自动扩展、禁止概念/素材、否定/方向错误及逐例结果。
- 传输超时、连接失败、HTTP错误、模型超时和明确降级分开；语义失败不冒充 no_match。P50/P95采用 nearest-rank，计入失败耗时，不悄悄删除慢样本。

报告保留输入字节摘要、固定版本、分母、分类指标、逐例实际意图、初次 HTTP、首次观察到的模型请求、后续串行延迟和进程资源读数。校准另列初次/后续推理；HTTP不声称首次请求一定是模型冷启动。它测串行认证HTTP与本次缓存状态，**不是**专题的1000素材/2000映射/5会话/200次混合浏览器交互、空闲/视频并行负载试验。真实机器与条件未冻结，因此这些项仍待验收。

`business_eligible` 需真实业务集、独立人工标注、独立校准绑定、明确批准门槛及最低类别数同时满足；`business_metric_gates_passed` 仅表示所测指标符合冻结门槛。`business_acceptance` 始终为 false，最终还需人工审查和完整 A/T/I 证据。脚本成功只表示报告生成，不代表标签、许可或医学结论获批。

## 已执行与待办

`tests/terminology/regression/` 检验分母、故障、禁止推导、分组/版本/素材池、索引哈希及校准隔离。`validate_http.py` 真实执行 CPU BGE 校准、三路 HTTP 与独立 CLI、模型超时/关闭的明确降级和精确短语保留、发布/旧整组回滚；使用明确标记的合成 PNG、几何词表和合成标注。私有报告在 `jobs/d16-validation-*/`；不记录原 token，不修改原素材或医学字段。

D16 状态为**工具工程已验证，真实业务与Windows指定负载待验收**。正式独立120条及另备校准集、阈值决定、真实剪辑人员易用性、并发/视频负载和最终目标机器均留待补充；不以合成样本替代。继续推进不依赖这些输入的 D17 公共语义契约。

本轮11项新增、项目`.venv`相关140项及普通/纯核心wheel环境各74项通过；pip check、compileall和JSON Schema/独立集预检通过。最终私有批次 `jobs/d16-validation-c19999a1/validation.json`：25次HTTP（对照报告16次）、两条校准、4条三路查询、两次CPU任务。BGE路线4条串行HTTP nearest-rank P50约156ms/P95约171ms，进程峰值约630MiB；这些数字仅为合成小批次诊断，不满足指定并发/业务验收条件。
