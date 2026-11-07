# D00 工程骨架与公共契约

日期：2026-11-07。内部协议版本：`d00-v1`。**D00工程验收通过**：六份原始文件已补齐，真实模板35/14列、说明表逐列顺序和字段归属已校验并绑定内容摘要。完整医学分类词表仍未发布，相关缺失门槛保留给D13；这不表示业务服务、模型或医学审核已完成。

## 工程与依赖

采用 Python src 布局、setuptools 构建、Pydantic 运行时校验及 pytest。当前支持范围为 Python 3.12，实际使用项目根目录 `.venv`；未测试其他 Python 次版本。包版本为 `0.1.0`，包版本、内部 schema、原始契约版本、taxonomy 和 lexicon 版本分别管理。

- [pyproject.toml](../pyproject.toml)：直接依赖及构建工具版本；目前不包含 FastAPI、FFmpeg、OCR、CUDA 等后续依赖。
- [requirements-dev.lock](../requirements-dev.lock)：Windows x86_64 / CPython 3.12完整开发依赖锁，含确切版本与wheel的SHA256；D00初版14个包，D02新增Pillow后为15个，D03扩展为27个；不含本项目editable路径。
- [源码](../src/media_tag/contracts/README.md)、[测试说明](../tests/contracts/README.md)。

在已有 `.venv` 中复现安装与测试（仓库根目录执行）：

```powershell
& .\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements-dev.lock
& .\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m pytest -q
```

本次实际从 PyPI 获取 wheel，按文件摘要生成锁；另在忽略目录 `cache/d00-check-env/` 创建干净环境，从 `cache/d00-wheels/` 离线按锁安装，并用构建的普通 wheel 验证。依赖缓存和验证环境不提交 Git，也不是正式部署环境。

以后变更依赖：先在独立环境解析所需版本并运行测试，用 `pip freeze --all --exclude-editable` 获取全部包的精确版本，用 `pip download --only-binary=:all:` 下载该平台的全部 wheel；用 `pip hash` 或 SHA256 为每个锁定项登记对应文件哈希，再在干净环境按 `--require-hashes` 重装验证。同步直接依赖与构建工具版本，不把带本机路径的 editable 记录写进锁。其他平台需单独解析、锁定及验证，不能直接放宽当前哈希。模型环境由后续任务单独评估。

项目继续采用现有 Python/FastAPI/SQLite/FFmpeg 工程方向；D00 只落地 Python 契约。人员页面拟定由 API 服务提供 HTML/CSS 与原生 JavaScript，按 `web/library`、`web/process` 等业务模块组织，轨迹编辑可使用浏览器 canvas；本轮不建立空 web 目录或安装 Node 工具链。实际页面与服务在对应任务实现，目标服务器及 AI 宿主仍须核实。

## 原始模板：只读加载，不内置正式表头

请把以下六个原始文件保持原样放入本地 `asset-index-template/`：

1. `README.md`
2. `素材库打标方案.md`
3. `feishu_asset_index_fields.csv`
4. `feishu_asset_index_template.csv`
5. `feishu_gap_table_fields.csv`
6. `feishu_gap_table_template.csv`

当前六个文件均已到位，继续只读且忽略Git；没有恢复历史副本或将原文复制进代码。用户文件未另带发布版本号，因此使用内容指纹作技术版本身份，不虚构业务发布号。复核本轮冻结的字节基线：

```powershell
& .\.venv\Scripts\python.exe -m media_tag contracts check-template --expected-fingerprint 40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af
```

加载器读取实际表头，保持顺序与原单元格，不写输入文件。读取UTF-8（允许BOM）和CSV引号/多行单元格；检查素材模板35列、缺口模板14列、非空且唯一的表头、记录宽度与两个规则文档非空。说明表必须包含“字段、飞书字段类型、选项/格式、填写方式”，字段行必须与模板逐列同序；缺行、重复、未知字段类型/归属规则均拒绝。缺口表声明共用素材表选项时，检查对应字段存在且类型一致。示例列不导入业务值。

返回六文件的字节SHA256和按文件名排序后的摘要集合指纹；默认技术版本为 `sha256:<fingerprint>`，也可用 `--contract-version` 显式添加标签。`--expected-fingerprint` 使任何源文件字节变动（包括BOM/换行）以 `template_baseline_mismatch` 拒绝；不指定时仍可检查新输入，但 `baseline_verified=false`。CLI输出摘要、数量、缺完整选项定义的字段名，不输出原始表格或示例。缺文件时退出码2并返回完整列表，不创建占位目录。格式错误、读取失败、路径越界分别记录错误码。摘要绑定实际读到的字节，不提供多文件事务快照；正式发布仍需冻结输入。

本轮真实基线指纹：`40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af`。它绑定全部六文件；仓库只保存实现、技术摘要及合成测试，不保存原始表头/枚举表的副本。后续换版必须重新核对并更新基线，不能只改版本标签绕过字节检查。

字段归属在内存中区分脚本、人工、人工/脚本、AI初稿抽查及AI建议逐条确认，原始规则文本保持不变。这是契约解释，实际写权限/修订保护仍由D01/D06实现。选项格式也保留原文，不按竖线、示例或括号注释猜出完整分类词表。

本轮发现：科室含省略项，器官/系统只给示例，疾病/症状仅指向锁定列表。`incomplete_selection_definitions`如实报告这些字段；不发布、不补写枚举。缺口表共用医学分类时同样继承这一限制。完整taxonomy、条件限定选项和别名仍须D13审核发布；暂缺正式taxonomy不阻止D00结构契约验收，但不能输出未经发布的医学标签。

因此本轮返回 `field_definitions_verified=true`、`baseline_verified=true`，同时保持 `formal_semantics_verified=false`：后者表示没有验证完整词表语义或医学事实。测试中的35/14列仍是临时生成的 `synthetic_*`；真实本地校验单独执行，不将模板搬进测试目录。

原始方案中的早期“飞书为人工字段主库、SHA256关联、首中尾抽帧”等描述，与当前总控的SQLite主数据、稳定身份、连续视频证据存在演进差异。原始文件保持不变，实际实现按当前总控和AGENTS约束；D00不恢复旧同步/预览策略，也不把示例原话或许可当已验证事实。

## 建议、有效值与证据

`models.py` 使用严格类型、禁止额外字段和冻结对象；外部 JSON 通过 `model_validate_json` 进入。列表在JSON中表示，在Python模型内部使用 tuple。不要使用可绕过校验的 `model_construct` 或 `model_copy(update=...)` 导入外部输入。

| 结构 | 已实现边界 |
| --- | --- |
| `FieldValue` | `observed`必须有非空去重值；`unknown`、`no_match`、`not_applicable`、`failed`各自保留状态/原因且值为null，不能冒充false |
| `TagSuggestion` | 仅ai/tool建议；有实际值时需证据ID及产生者版本；没有人工确认字段 |
| `EffectiveValue` | 独立的人工字段修订，要求操作者ID、正整数revision、证据及带时区确认时间 |
| `Evidence` | 绑定源SHA256、证据对象/摘要；视频/音频片段要求有效时间区间，抽帧要求源时间戳 |
| `ArtifactRef` | 相对引用及SHA256；拒绝盘符、URL、路径穿越、Windows设备名等。真实根目录解析及文件哈希校验属于后续存储任务 |

这是结构和引用检查，不能证明真实推理已发生、人工身份真实、许可通过或医学标签准确。`WorkerResult`不能携带 `effective_values`；D01已实现对象级授权、非医学文本人工修订与建议隔离，见 [索引与存储](catalog-storage.md)；任务租约已由[D03](router-queue.md)实现，完整审核采用仍待D06。尚未发布的taxonomy不能从通用字符串字段绕过正式标签验证。

静态图片或文件名证据不自动成为动态方向或医学依据。D00保留证据类型与区间，未实现连续视频观察、医学审核或面向特定字段的证据充分性判断。

## 任务与结果

`TaskEnvelope`绑定素材ID/revision、task/job、attempt、lease_epoch、输入指纹及引用、版本集合、目标字段、配额和输出前缀；最多总尝试次数默认3，attempt不得超出预算，GPU槽只能0或1。输出前缀必须为 `jobs/<job_id>/runs/<task_id>/attempt-<attempt>/`。

`WorkerResult`区分 `succeeded/partial/failed/not_applicable`，保留耗时、可选峰值内存、产物、证据、建议及错误；失败/部分结果必须带错误，不适用必须说明原因，失败/不适用不能返回标签建议。证据ID不可重复，建议不得引用缺失证据。

`validate_result_for_task`检查结果的task/attempt/lease_epoch/输入指纹/版本一致、仅包含请求字段、产物位于输出前缀或完全匹配已登记输入。**它不提供运行时队列**，也不验证当前租约是否仍有效、输入文件真实哈希、取消状态、可见范围或素材revision是否已变化；这些由D01/D03等任务实现。输入指纹由D03调度者按内容/证据/规则/模型/词表等完整输入计算，D00不把不完整字段的摘要当成正式缓存键。

可输出 JSON Schema，例如：

```powershell
& .\.venv\Scripts\python.exe -m media_tag contracts schema --model task
& .\.venv\Scripts\python.exe -m media_tag contracts schema --model result
```

其他名称：`field-value`、`suggestion`、`effective-value`、`evidence`、`vocabulary-manifest`。JSON Schema描述基本结构；跨字段条件和任务/结果匹配须同时调用Python校验，不能仅靠Schema判定通过。

## 词表入口门槛

`VocabularyManifest`分别标识taxonomy/lexicon、版本、状态、内容文件及SHA256；published要求发布者与带时区时间，lexicon还必须绑定taxonomy版本。实际读取时必须指定期望种类和版本；lexicon必须额外指定期望taxonomy版本。

```powershell
# 参数是未来本地发布清单的位置和真实版本；当前没有正式词表文件。
& .\.venv\Scripts\python.exe -m media_tag contracts check-vocabulary --manifest data/terminology/release.json --kind lexicon --version '待提供的词表版本' --taxonomy-version '待提供的分类版本'
```

此入口拒绝draft/review/rejected、缺失内容、摘要不符、版本不符、越界引用、坏JSON及重复JSON键。词表内容目前只检查为JSON对象；**未实现D13的逐词条审核、taxonomy引用、概念/映射关系校验、签名/发布者权限、发布事务及回滚**。返回 `semantic_review_verified=false`，通过入口不代表有权发布，也不意味着词条已经医学审核。

## 验证与待补验收

本轮关联R05/R10/R11、N03/N05，以及A06/A08/A09的D00工程前置。项目`.venv`中的72项合成/边界测试通过；真实模板与说明表35/14列同序、归属、共用字段类型及基线摘要校验通过，六文件校验前后字节摘要一致。真实素材处理、模型、完整分类词表、许可、医学审核及服务端业务验收未执行，不能据此认定A/T/I整体验收通过。
