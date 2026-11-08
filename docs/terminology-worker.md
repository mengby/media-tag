# D14 文本小模型与术语匹配 Worker

2026-11-08：实现固定 BGE、发布词表短语匹配、明确的候选/歧义/拒识/降级、文本索引与配对切换、账号隔离缓存，以及 D03 `terminology_resolve` Worker。D15素材检索/页面已实现（[说明](search-feedback.md)）；D16独立评测和实际医学词表仍是后续事项；本机工程批次只使用明确标记的合成几何概念。

## 模型、依赖与实际兼容性

采用 [BAAI 官方 BGE-small-zh-v1.5 模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)，固定上游提交 `7999e1d3359715c523056ef9478215996d62a620`。仓库只提交 `embedding/bge.lock.json` 中的来源/字节数/SHA256，不提交权重或上游 README。95,827,648字节 safetensors 约91.4MiB；本次没有重新下载 torch/CLIP。

七个文件均核验：大权重对照上游LFS SHA256，小文件对照上游Git blob摘要后生成SHA256。实际加载禁止网络和远程代码，使用 safetensors、BertModel/BertTokenizerFast、CPU float32、CLS、L2归一化和512维。查询固定前缀“为这个句子生成表示以用于检索相关文章：”，概念文档不加前缀；此文本向量与 CLIP 独立。

使用项目 `.venv` 的 Python3.12.14、torch2.9.1+cu128、transformers4.57.6、tokenizers0.22.2、numpy2.2.6、psutil7.1.3；BERT导入、CPU矩阵和真实中文编码已执行。沿用现有MSVC私有运行时准备，不修改全局Python。CPU编码不领取GPU槽，D14未实现CUDA推理；任务请求GPU会被拒绝。服务导入不加载模型、不下载、不启动后台进程。

没有新增包版本。`requirements-text.lock` 从已验证的61包视觉锁提取46包核心/开发/文字依赖闭包；`.[text]` 是新增可选依赖组。离线wheel或网络安装仍须按锁校验，torch的CUDA构建索引见原有视觉环境说明。27包核心环境可以运行字面词表和降级流程，不要求torch。

```powershell
.\.venv\Scripts\python.exe -m media_tag prepare-text-model --model-root cache/models/d14
.\.venv\Scripts\python.exe -m media_tag check-text-model --model-root cache/models/d14
```

前者是唯一显式下载命令，已存在但摘要不符的文件不会覆盖；后者实际编码两条中性几何文字与一个查询，输出维度、原始余弦、设备、耗时和峰值内存，不证明医学匹配。首次加载约数秒，驻留编码进程可复用；实际批次数据见验证报告。输入最多每批16条、单条8000字符/512 token，超限明确拒绝，不截掉尾部否定或方向。

## 启用与接口

沿用 [本地配置](../config/README.md)，须启用 D13：

```json
{
  "terminology": {"enabled": true, "managers": [], "publishers": []},
  "text": {
    "enabled": true,
    "model_enabled": true,
    "model_root": "D:/ai/media-tag/cache/models/d14",
    "query_timeout_seconds": 8.0,
    "build_timeout_seconds": 120.0,
    "min_cosine": 0.75,
    "min_margin": 0.05,
    "cache_entries": 128
  }
}
```

这只是功能配置片段，真实凭证、workers、路径/模板摘要沿用完整LocalConfig；模型启用时路径须为绝对本地路径。默认 `text.enabled=false`、`model_enabled=false`，没有默认人员或正式词条。审核、发布和素材医学审核角色保持独立，名单须由真实配置指定。

| 方法/路径 | 已实现行为 |
| --- | --- |
| GET `/terminology/resolve/status` | 返回D13发布指针、D14 serving整组、pending及版本/索引状态 |
| POST `/terminology/resolve` | 搜索文字解析，editor/reader/admin可用；不返回素材 |
| POST `/terminology/text-indices/activate` | 指定publisher构建或复用完整索引并CAS切换 |

公共查询只接受 `purpose:search`、`evidence_origin:human_input` 且无文件引用，防止冒称OCR/视觉证据。沿用Bearer/同源cookie、body上限、no-store与脱敏错误。query不写人工字段、词表或搜索日志；原文始终作为 `literal_query` 返回，供D15保留字面分支。

## 匹配与拒识

查询最多2000字符。保留原文、Unicode规范化后的字符到原文区间、重叠/嵌套短语、匹配路径及逐项上下文；ASCII词边界避免将 art 命中 heart。不能可靠映射罕见跨字符组合时明确提示，不编造区间。

精确标准名、旧名及已发布别名参与匹配。科室/器官条件只接受固定taxonomy选项ID；缺条件与条件不匹配分别报告。保留每个分句的否定和增加/减轻/静态/转换意图，方向相冲突、否定修饰方向或完整别名内含否定/惯用表达时保守标记歧义。不把related/metaphor/上下位变成同义闭包，不强选最长短语或第一目标。

只有唯一目标、条件满足且允许默认扩展的等价匹配才可 `resolved`；否定和没有明确对应规则的方向保留为歧义，不能直接扩展为正向医学标签。自动语境规则覆盖有限，不保证所有中文复杂句正确；用户显式上下文与原文并存，复杂否定/比较/方向须在D16用真实独立集评测。

未精确命中时可用 BGE 在当前配套文档中召回候选。标准名、定义、范围、旧名和允许的等价别名组成固定文档；不截断超长文档。原始余弦低于阈值可 `no_match`，分差小明确记录排名歧义。**BGE始终仅返回待确认候选，不自动resolved、不增加别名、不写医学标签**。0.75/0.05只是未校准工程配置，不是医学等价概率或业务通过门槛。

状态区分 resolved、ambiguous、no_match、degraded。模型离线/缺依赖/进程失败/超时/token超限/索引损坏均明确 degraded，仍保留原文及可用的精确词表；不把故障伪装成无匹配。明确关闭模型调用时只有字面结果，未命中可报告no_match。

## 索引、缓存与故障恢复

SQLite追加迁移8，不修改迁移1～7。D13 head表示已审核发布；D14有独立serving整组，固定 taxonomy release、lexicon release、BGE模型摘要、文本预处理版本、512维向量和文档摘要。版本均进入任务和缓存键。

激活请求含 `request_id`、`expected_generation`、`expected_serving_epoch`、taxonomy/lexicon release ID及note。校验D13配对和逐条发布记录，构建阶段不持有数据库写锁，分批CPU编码、独占文件写入/fsync，之后重新读取文件验证哈希、维度、有限值、L2范数、文档与版本。在一个最终事务中CAS切换整组；期间有新发布/回滚或其他激活则拒绝陈旧结果。

新D13发布尚未构建成功时，查询继续上一组词表与索引，返回pending，不将新词表配旧向量。首次尚无serving时，可只读当前已发布词表提供明确字面降级。D13回滚后可激活历史整组，重新验哈希并复用旧索引；serving epoch递增，旧缓存不复用。损坏索引可在新私有目录重建相同逻辑版本，旧文件不覆盖；失败保留旧serving并留失败批次。

文件在 `jobs/text-index-*/`，含README、inputs、documents、vectors.f32（little-endian float32）、index元数据及validation/failure。缓存最多128条（可配置），含调用者身份/角色、原文、上下文、来源、词表/分类/模型/索引/匹配版本和阈值；命中前重验快照与索引，降级结果不缓存。素材范围/索引修订缓存由D15另行实现，不跨用户复用私有素材。

交互查询直接走短超时CPU编码器，不排在视频渲染DAG后；索引构建为受控同步CPU批次，尚未提供独立异步lexicon_build队列。锁排队、首次加载、IPC和推理共享截止时间；取消/超时杀死并等待真实子进程退出，父进程死亡监视沿用D04。服务关闭清理驻留进程；不泄露原始异常/用户文本到公开日志。

## D03 Worker与来源

注册阶段 `terminology_resolve`，固定模型/preprocess版本，GPU=0，`target_fields=()`。ResolveTask固定分类/别名发布及可选index ID，request必须purpose=tag；沿用task/attempt/lease_epoch、输入指纹、版本、实际耗时与峰值内存、取消和有限重试协议。

human_input只证明明确的人工文字；old_filename须等于登记展示名，仍不是医学证据；prior_quote须来自现有人工有效值。OCR须对应父阶段实际返回的model_report，校验ref/字节哈希、源SHA256、报告kind及精确文字；没有指定ref时仅在唯一model_report可明确定位时采用。visual_description目前无D05生产者，明确拒绝，不能冒称已执行VLM。

输出私有 `terminology_report`，记录原文区间、概念候选、关系/路径/分数、上下文、源哈希/修订、来源定位和版本。没有TagSuggestion正式字段写入、没有人工确认事件，也没有词表发布权限。OCR文字不是视觉医学结论，报告始终 `medical_evidence_eligible:false`；D15/D06采用旧候选前仍须重验当前素材/分类和实际画面证据。

## 实际验证与后续

新增40项，项目`.venv`全量540项、27包纯CPU469项、普通wheel集成197项及4项JS通过。真实批次为 `jobs/d14-validation-20261108-91082c34/validation.json`：40次HTTP、两次CLI Worker、8条中性工程查询HTTP P50=110ms、nearest-rank P95=125ms；首次编码4.28s、峰值进程约636MiB。以上仅为该小样本工程诊断，提交状态见 [PROGRESS](PROGRESS.md)。测试包括真实中文CPU推理、512维/归一化/进程复用、真实超时/取消、字面/多义/否定/方向、类型/阈值拒识、离线降级、缓存隔离、索引故障/并发/回滚、Router报告与来源拒伪造、迁移恢复。合成协议夹具不冒充OCR调用，真实BGE诊断不冒充医学业务集。

仍缺正式医学taxonomy/候选来源、真实词表管理角色和独立标注查询集。D16的120条建议评测、准确率/覆盖率/错误疾病扩展和业务P50/P95尚未完成；D15页面与素材检索已实现；D17公共语义API及后续宿主联调由后续阶段继续实施。

D16新增解析策略摘要 resolution_policy_sha256，绑定模型开关、阈值、margin、查询超时和缓存容量，纳入查询版本及缓存键；配置变化不能伪装为同一冻结评测。实际独立效果及Windows负载边界见 [D16工具说明](terminology-evaluation.md)。
