# CT-23：原十字段独立评测工具

已实现可调用的离线数据/人工标注导入契约、训练/校准/验收分组隔离、实际 AnalysisDocument 导出读取与哈希核验、逐字段/模态指标、不可变报告和候选规则建议。复用 D16 的 `fraction` 与 nearest-rank `percentile`，沿用其固定字节摘要、独立组、失败不删分母和合成/业务分离原则。

本次只使用临时目录合成源文件、合成标注身份和明确的模型协议报告；没有读取生产数据/真实素材、调用模型/网络或修改数据库/API/config。没有正式业务集与真实审核人，**不把合成分数当准确率验收**。`calibrated`、`automatic_enablement`、`business_acceptance` 始终 false，包括未来输入声明为 business 时；工具不批准医学值、许可、隐私或自动精标豁免。

## 可运行入口

```powershell
.venv\Scripts\python.exe -m media_tag.topic_evaluation --schema dataset
.venv\Scripts\python.exe -m media_tag.topic_evaluation --schema export
```

输出严格 JSON Schema，不生成样本答案或审核人。按 Schema 准备真实来源的私有 JSON 文件后，使用以下**接口示例**运行；示例路径需要替换成实际、已授权冻结文件，不表示本机已有正式评测资料：

```powershell
.venv\Scripts\python.exe -m media_tag.topic_evaluation --dataset PRIVATE_DATASET.json --export PRIVATE_EXPORT.json --split acceptance --output EXISTING_PRIVATE_OUTPUT_DIRECTORY
.venv\Scripts\python.exe -m media_tag.topic_evaluation --dataset PRIVATE_DATASET.json --export PRIVATE_CALIBRATION_EXPORT.json --split calibration --output EXISTING_PRIVATE_OUTPUT_DIRECTORY --propose-rules
```

Python：`evaluate(dataset_path, export_path, split='acceptance', output_directory=None)` 返回 `report/path/sha256`；`propose_rules(calibration_report, minimum_groups=5, minimum_exact_precision=.99)` 只产候选。输出目录必须已存在且本地，报告按内容摘要命名、不覆盖旧报告，已有同名文件须完整 hash/大小一致；不写任何线上阈值或配置。文件为 UTF-8 JSON，CLI 输出也固定 UTF-8。

## 数据与人工标注冻结

`Dataset` 的 `purpose` 必须为 `synthetic_engineering` 或 `business`。合成身份必须显式 `synthetic-*`；业务声明不能复用合成身份，且必须声明独立人工标注。冻结时间须含时区，记录标注人、裁定人、总体/逐样本标注依据；**工具无法验证这些人和独立性是否真实**，报告明确保留“未由工具验证”，不据字符串自动批准。

每个 case 固定：稳定 case ID、完整源 SHA256、模态 image/video/animation/audio、近似来源组 `near_group`、training/calibration/acceptance 拆分，以及原十字段完整 gold。相同源 SHA 不允许重复计数；任意训练/校准/验收拆分之间，相同 SHA 或相同 near_group 均拒绝。近似派生、转码、裁剪、同段改速和同源截图应由数据准备者归到同一组；工具检查声明，**不重新运行判重模型，也不能识别被错误分到不同组的隐性相似样本**。公开预训练语料是否出现同素材未知，不能宣称防住全部预训练泄漏。项目训练资料是否已完整列出单独保留声明。

每字段 gold 状态为 labelled/unassessable/not_applicable。labelled 必须显式 `complete_value_set=true`，给出人工已经穷尽的有效值集合；无正标签可显式空集合。未能标注则 unassessable，不能混充空标签。可人工列 `forbidden_values` 与 `opposite_direction_values`；正确与禁止/反向集合不得矛盾。方向错误按这些**事先标注的精确标签**计数，不根据字符串猜方向或自动推导同义词。

纯音频的画面主体、表现形式必须 not_applicable；其余字段、所有视觉字段均由独立人工 gold 逐项判断适用性，不能按模态强制填标签。生活场景的科室/器官/疾病、普通图片情绪、普通 BGM 的医学字段等可有据标 not_applicable，保留具体 annotation_note；不能硬改为 labelled 空集合。十字段 gold 均须出现一次。结构必需评估数固定为视觉 10、音频 8，与独立 gold 的实际适用标签分母分开；合法 NA 不减少结构评估数，也不能以模型缺能力或缺资料伪装人工 NA。

每个对照 run 冻结可接受的 producer pins：完整 `Versions`（规则、预处理、词表、模型 revision 等）和 prompt version。一个 run 可列多个明确 pins，以评测本地/精标混合流程，但不能临时接受未冻结的版本。数据集和导出都需匹配发布词表 version/SHA；缺词表须显式 null，不凭空生成正式词表。评测按字面已冻结标签评分，不执行术语扩展或替数据准备者新增枚举；原表头/词表保持原契约。

## 明确预测导出包

`Export` 绑定数据集实际字节 SHA256、purpose、run ID、词表 version/SHA、含时区导出时间；每条 prediction 绑定 case ID、源 SHA、模态和实测流水线耗时。结果必须二选一：`topic_analysis_document` ArtifactRef，或明确 failure_code。缺失预测仍进入评测分母，不用补造 unknown 结果。模型自报 confidence/accuracy 字段不在 Schema 中，直接拒绝。

导出目录是私有、明确冻结的文件包：文档、实际 evidence 文件和 producer 报告均采用相对路径和 SHA，禁止 `..`/绝对/逃逸路径。工具实际读取并校验 AnalysisDocument、证据和 producer 报告摘要；观察/候选和 NA 判断均要有实际证据和 producer 引用。正确 NA 须绑定同模态实际图像、音频段或连续视频段，单凭文件名/自报 NA、缺 provenance 或 NA 同时保留正标签候选都不能计正确。没有证据、producer 缺失/被改动或文档损坏不能算正确；失效不会回退旧成功文档。源/模态或模型/规则/预处理/prompt/词表 pin 变化使本次对照拒绝，避免混版本算指标。

离线 `evaluate` 验证的是**导出时的离线字节与契约**，不查询当前数据库、Router 权限或释放状态，也不重解码原始素材或通用解析每一种 producer 协议。下述 `freeze_catalog_export` 在冻结时另行执行当前对象授权、FileGate、登记源实际 hash、当前投影及报告限定验证；离线报告不能把这些快照当作之后仍有效的访问批准，不能把 provenance 当医学或来源批准。

单个导入文件上限 8 MiB；artifact 每文件 16 MiB、总读取预算 256 MiB，失败对象缓存，防止对同一损坏大文件重复扫描；私有报告上限 64 MiB。严格拒绝重复 JSON 键、NaN/Infinity、重复 case/run/来源、矛盾标注和不完整 pin。

## 当前目录的授权冻结导出器

已实现只读函数，免去人工拼装 document/evidence/producer 文件。函数只使用调用者明确提供的 case ID→asset ID，不从文件名、路径、相似分数或源 SHA 猜素材；不启动分析、修复、模型或同步任务。

```python
from media_tag.topic_evaluation import freeze_catalog_export, FreezeOptions, evaluate

bundle = freeze_catalog_export(router, frozen_dataset_path,
    case_assets={'ACTUAL_CASE_ID': 'ACTUAL_ASSET_ID'}, run_id='FROZEN_RUN_ID',
    principal=current_authenticated_actor,
    private_output=existing_private_output_directory, split='acceptance',
    options=FreezeOptions())
report = evaluate(bundle['dataset_path'], bundle['export_path'], split='acceptance')
```

这是可调用接口示例，变量需来自实际已授权上下文；不能临时编造管理员、审核人或模型调用。尚未新增 HTTP 导出端点或部署入口。调用者必须选择私有目录；输出强制与当前 Router workspace 和登记本地后端根目录互不包含，不进入 FileService 的素材服务目录，不生成公共 URL。原件验证仅只读，不额外复制原件；如果当前被验证的模型证据本身是完整输入字节，冻结包会包含该证据，因此**整个包必须保持私有，不能直接作为可公开素材发布**。

冻结步骤：

1. 校验真实身份对象 read 权限和普通 `FileService` 的当前字节门槛，保留当前 source SHA、owner/revision、权限、派生链/审核状态、当前 intake policy、intent/job、活跃任务/epoch/released 和 ReviewPolicy 元数据 fence。仅取调用 actor 自己当前的主线 intent/job，不借其他账号的成功缓存；以前有编辑权限、目前仅有 read 的同一 actor 可导出自己的既有结果。
2. 对 FileService 当前优先选择的**本地登记源后端**实际读取并重算完整 SHA/大小，复制后再次复核；不联网访问远端后端，不用完好的模型缓存回退损坏的登记源。
3. 复用 `TopicProjection.build` 的内部原文档，以及当前 `visual_fields`、`local_topics`、`precision`、`audio_topics` 的限定验证。`audio_topics` 必须调用 `verified(..., layer='local_audio')`，精标用 `layer='precision'`，阶段/layer/原始 reply 重算不能互换。任务须当前 active/succeeded/released、source/policy/prompt/model pin 和 accepted result 摘要一致；新 pending 或失效结果不回退旧成功。未接入的 `temporal_topics` 明确返回不可导出，不从独立包编造主线 producer；音频无合格主线结果时保留明确未就绪失败。
4. 只从当前已验证 producer 的 WorkerResult artifacts、必要 technical report 和文档引用中做有界复制，不扫描目录或打包日志。去掉未被当前字段引用的历史 producer；保留所有实际值/unknown/no_match/failed/NA，不为完成度补值。原 producer 身份、版本、任务、lease、原报告摘要不改；外层 evidence/report ref 换成内容摘要相对文件名，冻结文档取得新的 snapshot ID。
5. **原始 producer 报告逐字节复制，报告内部 ref 保持原值**，不改内容再冒充同一摘要。`freeze.json` 分别记录源 projection 摘要、外层冻结文档摘要、原 artifact SHA→包内文件映射和 export 摘要。离线工具使用包内引用/摘要；不要按原报告的生产路径去读文件。
6. 长读后重验登记源与限定报告，所有文件写入和落盘校验完成后，再以独立短只读事务复核 source/权限/FileGate/政策及完整 metadata fence；一致才立即原子发布 `bundle-*`。途中撤权、隐私门槛改变、源/报告/政策/元数据变化或预算失败会删除本次平面 staging 文件，不留下有效导出入口。目录和数据库不写生产记录，未完成案例不会伪造 producer。

默认限制：32 cases、单证据/报告 16 MiB、登记源 64 MiB、bundle 256 MiB、包含验证与落盘复读的总读取预算 512 MiB、256 artifacts、60 秒软时限。元数据在只读事务中取得，文件验证不使用 writer 事务。最终复核是一次乐观快照，不持有数据库写锁，也不承诺撤权能够追回已经授权导出的本地私有副本。

需要按模态或多 run 分包时，在**数据集冻结前**填写该 `Run.case_ids` 范围；映射必须完整覆盖该 run 在所选 split 中的案例，不能看完分数后临时少给难样本。默认 `case_ids=null` 兼容原全数据集行为。报告记录实际 evaluated_case_scope；比较多个 run 时还须核对范围一致。冻结 `Export.split_scope` 防止把 acceptance 包改用来调 calibration。

冻结导出的 `elapsed_seconds` 是**当前被采用 producer 的实际 WorkerResult stage 耗时之和**，标为 `sum_verified_producer_stage_seconds`，不是整条流水线墙钟、导出耗时或冷启动测量。未就绪案例为 `elapsed_seconds=null/timing_scope=not_measured`，不会伪填 0 秒模型运行；旧手工导出的 `declared_pipeline_elapsed` 仍兼容。离线报告按 timing_scope 分开列时延，缺测不进入时延分母。

旧 Prediction/Export 若省略 timing_scope 且耗时缺省或为 null，自动归为 not_measured，不编造 0 秒；显式矛盾的 scope/耗时仍拒绝。若存在多种**有值**计时口径，全局时延标 `mixed_timing_scopes`，不合并计算总和/P50/P95；`latency_by_scope` 继续分别真实计算。仅有一种有值口径时保持原分位数行为，未测案例不会制造混合口径。

真实音频的两个结构性视觉 NA 原样保留；如果主线没有为其提供 producer，导出器不补造。这两项不会计有证据的正确 NA，也不冲掉音频八项结构必评。合成 WAV/Bridge 回归目前保留三项 observed、五项 unknown、两项无 producer 的视觉 NA；不把全八字段已评估误称为八项都已完成。

## 分母与指标

- `total_field_assessments`：每份素材完整十字段状态；`structural_required_evaluations`：视觉 10、音频 8，纯音频两项结构性视觉 NA 另计，不随其他合法 gold NA 缩水。
- `applicable_fields`：按独立 gold 排除实际 not_applicable 后的标签覆盖分母；人工 unassessable 仍保留覆盖分母，不能当作已知正确或错误标签/适用性。
- `structural_assessment_success`：结构必需项中的准确标签 + 有实际 evidence/producer 的正确 NA，除结构必需评估数。生活图四项合理 NA 仍须完成 10 项评估，而不是只评 6 项就完成。
- `correct_not_applicable`、`incorrect_not_applicable`、`missed_not_applicable`、`incorrect_applicable_claims`、`unverified_not_applicable_claims` 分别保留正确 NA、假 NA、漏判/未完成 NA、NA 上错误正声明和无证据 NA；适用性判断正确率/错误/未解决独立列出。
- `exact_match_labelled`：有独立完整人工集合的字段中，实际 observed 且证据/producer 完整、预测集合与 gold 完全一致的比例。
- `end_to_end_exact_coverage`：上述正确数除全部适用字段，揭示未标注或未输出的缺口。
- label precision/recall/micro F1 与平均 Jaccard：按字面集合；弃权/失败保留召回分母，证据缺失的声明不能获得 TP，但仍保留所声称预测标签的分母。Gold NA 上错误 observed 标签也进入 label precision 的 FP 分母，不因 NA 从指标里消失；平均 Jaccard 仍只针对 labelled gold 的标签集合。
- unknown/no_match/failed，以及适用字段上的 not_applicable **都不是成功**。人工负样本的 unknown/no_match 仅记 `appropriate_negative_abstentions`，即使期望集合为空也不把弃权算完整标签成功；若正式词表有明确“无”值，可由人工把该正式值标入 gold。
- unknown/no_match/failed 面对 gold NA 也不算正确 NA；模型 NA 面对 labelled gold 是假 NA，不能获得标签匹配分。正确 NA 单独计分，不冒充 observed 标签成功。
- 医学额外标签按字段案例与标签数量独立统计；未匹配医学候选的额外标签另列，**包括 gold NA 上所有医学正标签/候选**，不只扫描 applicable/labelled 项。反方向已输出值、反方向未匹配候选和禁止声明也各自统计，不能藏在总体准确率中。
- 按字段、模态、字段×模态列覆盖/精确匹配/集合指标、弃权、执行失败、证据/producer 缺失、待人工/词表/模型/证据等 `next_step` 和具体 reason 分布；逐例保留 source SHA/近似组及实际值，便于复核。
- 近似组数量单列，指标仍是样本加权；不把同组变体数当统计独立样本，也不生成虚假的置信区间。

pipeline P50/P95 复用 D16 nearest-rank，包括有耗时记录的失败；缺失预测没有计时则明确不进入计时分母。耗时为导出者实测声明，不证明模型冷启动或机器负载；工具另实测本次读取/评测耗时（不含最终报告落盘），不把它混称模型时延。

## 候选校准边界

`propose_rules` 拒绝 acceptance 报告，CLI 也提前拒绝在 acceptance 上开 `--propose-rules`。候选阈值是**实测完整匹配率和独立已观察校准组数量**，不是模型自报信心；未解决组不能充当已观察支持量。证据/producer 缺失、失败、方向错误或支持不足均阻止候选；医学四字段始终保留人工确认，不能提成自动免审核规则。

候选明确限定 observed、无未匹配候选、当前冻结 producer/词表、完整可验证证据和非医学字段；unknown/no_match/failed 继续分流精标，不因为某个桶校准分数高而跳过。已输出/候选的反方向和禁止声明都会阻止建议。

校准精度以运行时能检查的 observed 前提构成分母：**gold NA 上的错误 observed 也必须进入失败分母**，不能通过只筛 labelled gold 抬高本地免精标建议。Gold unassessable 上的合格 observed 无法判准确性，会明确阻止该桶候选，而非静默筛掉。只有符合候选运行条件且人工答案可判定的独立组可提供支持量；NA 错误观察提供的是失败支持，不能获得成功数。

合成用途和项目训练范围不完整都会阻止候选。即使未来 business 数据满足某个字段/模态桶的门槛，输出也仅为“需要独立验收与人工批准的候选”，不写 calibrated=true、不自动免精标或修改线上阈值。正式规则需真实独立业务集、真实角色与标注核验、独立验收、代表性覆盖和另外明确的发布决定。本轮没有提供这些条件。

## 本次验证与待办

```powershell
.venv\Scripts\python.exe -m pytest -q tests/topic_evaluation
.venv\Scripts\python.exe -m pytest -q tests/topic_evaluation tests/terminology/regression
```

测试只有合成 PNG/WAV/APNG 源、显式 synthetic 标注身份、未执行模型的 producer 协议和临时文件；包括实际独立 CLI、不可变结果、10/8 结构评估数与独立 gold 适用分母、弃权/负样本/无人工答案、医学假阳性与方向错误、实际 artifact 损坏、split 泄漏与 pin 漂移、confidence 拒绝、候选支持量及禁止验收集调阈值。真实业务、真实模型调用和真实标注验收均未发生。

2026-11-11 首轮验证：`tests/topic_evaluation tests/terminology/regression` **41 passed in 2.60s**，包含本包 30 项与 D16 原回归 11 项。适用性修正后增加生活视觉/BGM 的逐字段 NA、结构 10/8 计数、正确/假/无证据 NA、NA 上医学输出/候选 FP、NA 错误 observed 的校准分母及未知 gold 保护回归。测试有重叠时不累计计数。

适用性修正最终验证：同一命令 **58 passed in 2.87s**，包含本包 **47 项**、D16 原回归 **11 项**。生活视觉可四项 gold NA 而结构仍评 10 项；普通 BGM 可六项 gold NA 而结构仍评 8 项；有证据/producer 的正确 NA 可完成结构评估，错误医学输出与候选仍分别计 FP，Gold NA 错误 observed 不从校准失败分母消失。

2026-11-12 冻结导出器验证：`tests/topic_evaluation tests/terminology/regression tests/audio_topics/test_projection.py` **90 passed in 48.64s**。包括实际隔离 ASGI HTTP 鉴权状态、内部目录冻结→离线评测、只读 actor 自身结果、真实 ACL 中途撤权、真实未复核衍生副本门槛、源对象损坏/中途改变且不回退模型缓存、政策/元数据变动清理、未释放/损坏/新 pending 不回退旧成功、local_topics 保留 unknown、真实合成 WAV/Bridge 的 audio_topics layer/reply 重算、结构 8/视觉 NA 不造 producer、冻结 run/split 范围和原数据库全表 dump 不变。原报告字节和 export 摘要均校验；所有媒体/身份/Bridge 仍为明确合成协议，真实模型/GPU 调用为零，未接生产实例。

计时兼容和混合口径修正后，同一综合命令 **97 passed in 51.57s**。新增旧 JSON/Export/Python 未提供耗时的兼容、显式矛盾拒绝、混合口径不计算全局分位数、单一口径与未测样本兼容回归；不涉及用户暂缓的全库复用、连续视频全链路或迁移/综合故障/负载工作。

仍待真实独立人工资料、真实角色核验、完整近似来源组/项目训练范围、正式发布词表与配套导出、实际对照运行、候选规则的独立验收及人工发布。工具工程不能替代 CT-23 业务校准和最终签收。
