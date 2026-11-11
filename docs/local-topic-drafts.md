# CT-12：本地十字段初稿核心

本次交付静态图片、静态贴纸的独立可调用核心，以及 Router stage/handler/子进程/报告验证与投影合并接口；共享 Analysis/API/config/调度接线由主流程另行完成，不能据此声称已接入生产队列或当前人员页。未运行真实 GPU/权重，不表示十字段的真实模型质量或业务验收已完成。复用项目已选择的 `Qwen/Qwen3-VL-2B-Instruct`，并没有另选后续精标模型。

## 现有能力复用

- `semantic.registry.SemanticRegistry`：包内固定 manifest、模型 revision 摘要和本地全部权重校验。禁止自动 fetch/下载。
- `models.native.prepare_native_runtime`、`semantic.processor.make_processor`、`semantic.patch_projection.replace_patch_projection`：现有本机 Qwen 加载和预处理路径。
- `review.policy.ReviewPolicy`：原始表格字段定义、已发布且固定摘要的词表、单/多选与值校验。
- `analysis.contracts.AnalysisDocument`：原十字段、医学建议、实际证据以及未批准标志；不改变原 35/14 列契约。
- `FilesystemBackend`：本地路径边界、只读原件、有哈希的不可变输出。

## 调用接口

调用者先创建**已有的独立私有工作目录**，提供当前已验证的 `ReviewPolicy` 和本地原件后端。以下为可调用 Python 接口；具体生产调度接线后续由主流程实施。

```python
from media_tag.local_topics import LocalQwenGenerator, LocalTopicService, Options
from media_tag.storage.filesystem import FilesystemBackend

generator = LocalQwenGenerator(model_root, enabled=False)
conditions = generator.readiness()  # 不导入 torch、不探测 GPU、不读取权重

# 仅在后续已协调独占 GPU、有界子进程/总时限的验证任务中显式启用：
generator = LocalQwenGenerator(model_root, enabled=True)
service = LocalTopicService(private_workspace, policy=current_review_policy,
                            generator=generator, options=Options())
result = service.run(FilesystemBackend(original_directory), relative_source_key,
    expected_source_sha256=source_sha256, asset_id=asset_id,
    asset_revision=asset_revision, analysis_id=analysis_id,
    content_kind='image')  # 静态贴纸也可填 'sticker'
```

`enabled=False` 调用生成器会明确拒绝。`readiness()` 仅列条件，不声称模型已安装、权重正确、GPU 可用或模型已运行。模型开启时使用 `local_files_only=True`、`trust_remote_code=False`，先复核固定权重，再加载一次模型，十字段逐个生成，不把十张图片同时送入 GPU。输入只包括实际规范化图像、系统规则、字段问题和词表选项，不包括原件路径、原文件名、来源或许可。

服务自身是同步库，不创建资源租约、进程树或硬超时。**生产/实际验证必须由调用方放进现有有界 Worker，协调 GPU、总时限及进程树清理**；未完成此接线前不能直接作为在线入口。不得同时与生产 Worker 竞争 GPU。本轮没有执行上面的启用调用。

## Router 接线接口（已实现）

以下接口位于本包，外部 Analysis/API/config/executor 调用点仍由主流程接线：

- `runtime.stage(registry: SemanticRegistry) -> StageDefinition`：阶段名由调用者注册为 `local_topics`，固定当前 registry revision、`PREPROCESS_VERSION`、`PROMPT_VERSION`，只允许原十字段，强制 GPU 租约 **1**。
- `contracts.LocalTopicDeployment(enabled=False, model_root='cache/models/semantic')`：路径仅服务配置使用；没有合成执行开关、没有网络下载配置。
- `contracts.LocalTopicOptions`：继承所有 `Options` 预算，加严格 `model_revision/policy_digest`，禁止额外键。
- `runtime.LocalTopicHandlers(router, worker, deployment)(lease, stopped)`：当前租约读取真实原件，快照与规范化输入均位于当前 attempt；固定当前 ReviewPolicy/词表、模型、资源与版本后，独立 runner 执行实际 Qwen。
- `projection.verified(router, row, asset) -> (task, worker_result, router_report, report_ref)`：供 repair/投影/草稿索引共享使用。
- `projection.merge(router, connection, asset, kind, job, fields, evidence, producers)`：与精标合并接口同形，返回 `(fields, evidence, producers, stage_states)`。只接受 `kind='image'`；静态贴纸按图像证据处理。

任务版本须使用 `rules_version='sha256:'+policy.digest`、`preprocess_version=PREPROCESS_VERSION`、当前 `taxonomy_version/model_revision`，`StepSpec.prompt_version=PROMPT_VERSION`。目标字段可为原十字段的非空子集；模型仍执行完整十字段协议，建议与投影仅采用当前任务目标字段。所有字段版本绑定实际任务，合并时生成当前任务/lease 的 `AnalysisProducer`。

handler 的单次软时限覆盖原件读取、规范化、runner 和输出。runner 等待 `start.ready`，父进程先成功加入 `ProcessTree` 再开启 gate；监视父进程，限制继承环境、离线模式、请求/响应大小和日志（2 MiB）。取消/超时/日志越界时先终止整棵进程树；分配失败也杀掉未入树根进程并 wait。GPU 租约由现有 Router/LocalExecutor 保持至进程停止并 release，handler 不自行提前释放资源。错误只返回固定代码，不返回本地模型路径或原文件名。

`RouterTopicReport` 绑定任务 ID、lease epoch、输入 fingerprint、版本，以及：原件快照 `local_topic_source`、runner 实际读取 `local_topic_native_input`、规范化图 `local_topic_model_image`、逐字段原答 `local_topic_raw_report` 和最终 `local_topic_report`。所有引用相对 Router 私有 workspace，位于当前 attempt，包含完整 SHA256。内容对象短文件名用于避免 Windows 深层 attempt 路径过长；复用仍检查完整 SHA256/大小，冲突拒绝且不覆盖。

`verified` 重验数据库当前 active/succeeded/released attempt、未取消的 job、源 SHA、当前 spec/envelope/result、已发布词表/policy、prompt/model/version、所有输出文件摘要与建议/证据一致性。再从原件快照**重新规范化图像**并比对输入图摘要、从原答**重新解析十字段**并比对最终字段；不能把来源正确但输入图不同或字段值被替换的结果当作有效。生产 handler 与 `verified` 都明确拒绝 `synthetic_protocol`；测试中的 mock 原生协议只验证接线，不声称权重或真实推理有效。

当前任务未 release、失败、当前输出缺失/篡改、模型/词表/prompt 变化均保留待处理或不可用，不回退旧成功任务。`merge` 只读取指定 job 的当前 active attempt；当前结果失效会覆盖旧显示值为明确 pending/failed，不保留之前成功建议。内部报告和证据引用必须经既有 API 脱敏投影使用，不能将私有原件、模型根目录或 runner 日志当作公共下载入口；FileService 门槛不变。

## 十字段与分流

严格依次处理：用途大类、科室、器官/系统、疾病/症状、画面主体、过程/状态、表现形式、中文检索词、英文检索词、情绪/语气。每字段独立 JSON 对象，键严格为 `field/state/values/dynamic`，不接受额外键、重复键、字段错绑、代码块、未知状态、非字符串、重复值、空观察或越界答案。协议错误记 `failed` 并留待精标，而不是补造值。

| 结果 | 字段状态与下一步 |
| --- | --- |
| 正式选项逐字匹配且基于图片 | `observed` AI 建议；医学字段 `human`，其余 `precision` |
| 缺已发布词表或候选不匹配 | `no_match`，保留原词候选，`vocabulary` |
| 不确定 | `unknown`，`precision` |
| 动态/方向性推断 | `unknown`，`evidence` |
| 协议或字段类型不合法 | `failed`，`precision` |

如果完整选项超过提示预算，不截断成自选枚举；提示说明候选模式，服务仍按完整当前词表匹配。没有发布词表时不得把模型原词变成正式选项。

中文/英文检索词允许模型返回多个短语；原索引定义为标量文本时以中文分号连接成单个有效建议值，原始短语数组仍完整留在报告中，不改变表头或字段类型。

静态图的 `过程/状态` 每次也真实发问，但本版保守保留 `unknown + evidence`：当前选项只是字符串，尚无经过批准的“静态状态/动态过程”标记供程序核验。模型自报 `dynamic=false` 不能证明时间过程；原始答案完整保存在报告中。开放可确认的静态状态需要后续正式词表属性和专项验证。动态图/多页图明确拒绝，交给连续证据能力，不把首帧冒充全段完成。

医学四字段永远只是建议，不覆盖人工有效值。本服务无目录库写接口，不写审核人、许可、人工确认、隐私批准或有效字段。其他观察也没有已校准的精标豁免；模型自报信心不能跳过精标或人工审核。

## 原件、图像和报告

本地原件按指定 SHA256 全字节核验，限制字节/像素；源目录与输出根目录必须互不包含。PNG/JPEG/GIF/WebP/BMP/TIFF 中只接受单帧 8 位常见灰度/RGB/调色板输入；高位深、CMYK、ICC 颜色转换、动态图和多页图明确拒绝。EXIF 方向先规范化，透明贴纸在白色背景合成后变成 RGB 输入，保留明确处理版本；不会读取透明像素的隐藏 RGB 作为可见内容。图像最长边不超过 2048，再由现有 processor 执行配置像素预算；原件保持不变。

实际送给模型的 PNG 作为不可变对象存储，报告绑定原件 SHA256、该 PNG SHA256、完整逐字段答案、精确问题、prompt 版本、模型 manifest 摘要、预处理版本、当前 ReviewPolicy 摘要和已发布词表版本。服务核验生成器返回的图像/模型/prompt 绑定后才形成十字段 `AnalysisDocument`。`synthetic_protocol` 与 `local_qwen` 明确区分。返回的 `draft_attempt_complete=true` 仅表示十字段尝试都已记录；不代表 `analysis_complete`、医学/隐私确认或可下载/发布。

报告与模型图像另存于私有工作区，按内容摘要复用，原件不重命名、不覆盖。不会调用 FileService 或改变任何原下载/权限/隐私门槛；后续目录集成必须保留这些门槛。

## 验证与尚缺条件

实际验证命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/local_topics
```

测试只使用临时目录合成图像和 mock 协议；包括原十字段、已发布/缺失词表、医学人工值不变、原件不变、严格 JSON、静态动态边界、透明与 EXIF、源路径/哈希/预算、生成器绑定、默认禁用模型及完全 mock 的本地加载/逐字段调用接线。真实模型调用次数为零。

2026-11-11 本次隔离回归：`tests/local_topics tests/image_processing tests/search/test_visual_fields.py` 合计 **84 passed in 62.98s**；其中本地十字段包 27 项，图片模块 32 项，旧四字段回归 25 项。报告选项记录补充后，本地十字段包再次 **27 passed in 19.61s**；最后对显式布尔启用标志的补充验证 **1 passed in 0.52s**。这些测试有重叠，不累计计数。

Router 扩展后，同一综合集 **97 passed in 73.76s**；再补 runner gate/绑定和一致性篡改回归后，`tests/local_topics` **47 passed in 34.65s**；最终报告预算补充后重跑 **47 passed in 35.63s**。其中两个测试运行真实隔离的睡眠进程及子进程，只验证超时/进程树清理与分配失败根进程回收，不执行模型。最后一组原答/字段与原件/规范化图一致性测试，会同步重写报告及 WorkerResult 的摘要，仍被共享 `verified` 拒绝，验证不能仅靠报告自报的哈希关联蒙混通过。

仍待：共享主流程的队列/API/配置/索引接线、独占 GPU 小批实际验证、真实 JSON 成功率/十字段收益评估、正式医学词表与审核人、静态状态词表属性、代表性校准、与后续精标及人工复核的端到端验收。不能以协议测试替代这些条件。

## 主流程接线补充

本包现已接入默认关闭的`LocalConfig.local_topics`、原Router/配置执行器、全类型分析意图的静态图片分支、只读状态和显式补缺修复。启用时必须同时有ReviewPolicy，模型根目录为绝对本地路径；不下载模型。新静态输入派发原十字段，替代同作业旧四字段重复GPU推理；OCR/人脸/CLIP仍按原可选配置，视频/动画继续独立待连续能力。

合格本地十字段已与精标共用草稿检索、原35列可选AI导出和生产者元数据栅栏。医学四字段仍只进入质量侧表，无人工有效值的CSV医学列为空。`analysis.scopes.latest_topic_fields`在状态与检索中统一最新active逐字段范围，人工采用扩展另批复用同一规则；新待办/失败/取消遮蔽旧值，显式重跑local或precision均不回退另一阶段旧成功。已有人工值/unknown/拒绝继续优先。没有校准的本地免精标规则，静态过程保留待连续证据。

投影与修复先严格检查五份产物的类型、当前attempt路径、实际大小和有界哈希：报告/raw各1MiB，规范化模型图/native图各16MiB，原件按Options.max_input_bytes。16MiB与核心2048像素最长边RGB PNG及runner一致；合成2048高熵JPEG经规范化为超过4MiB PNG，实际Router协议采用通过。源/规范化图/原答再计算仍必需，不采用自报摘要。

父代理新增合成主流程回归覆盖登记→探测→派发→十字段→状态HTTP→检索/CSV、损坏raw报告修复保留probe、真实配置执行器未知图片探测采用、local/precision新范围交叉遮蔽。所有模型响应明确为mock协议，不是实际权重结果；本轮真实GPU/模型调用仍为零。人员页整体验收和生产启用未完成。

父代理关联回归：`tests/local_topics tests/analysis tests/precision tests/topic_index tests/export tests/search tests/upload/test_analysis_repair.py` **257项通过（235.36秒）**；新增主流程/跨阶段范围专项4项通过（8.50秒），批次重叠不相加。私有证据`jobs/ct-local-topics-mainline-20261111-d8d7e94a/validation.json`。
