# D00 工程骨架与公共契约

日期：2026-11-07。内部协议版本：`d00-v1`。**工程骨架已实现；D00 整项尚未验收**，原始模板缺失，正式列名/枚举/字段归属未冻结。本页命令已经用于本地结构检查，不是素材服务、模型推理或部署命令。

## 工程与依赖

采用 Python src 布局、setuptools 构建、Pydantic 运行时校验及 pytest。当前支持范围为 Python 3.12，实际使用项目根目录 `.venv`；未测试其他 Python 次版本。包版本为 `0.1.0`，包版本、内部 schema、原始契约版本、taxonomy 和 lexicon 版本分别管理。

- [pyproject.toml](../pyproject.toml)：直接依赖及构建工具版本；目前不包含 FastAPI、FFmpeg、OCR、CUDA 等后续依赖。
- [requirements-dev.lock](../requirements-dev.lock)：Windows x86_64 / CPython 3.12 的完整开发依赖锁，包含14个包的确切版本与实际下载 wheel 的 SHA256，含pip/构建/测试工具；不含本项目 editable 路径。
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

当前六个文件全部缺失。目录继续忽略 Git，代码不从历史提交自动恢复或复制原文。使用收到的真实契约版本标识执行以下命令；`待提供的契约版本` 是需要替换的参数，不是已发布版本：

```powershell
& .\.venv\Scripts\python.exe -m media_tag contracts check-template --contract-version '待提供的契约版本'
```

加载器读取实际表头，保持顺序与原单元格，不写输入文件。读取 UTF-8（允许 BOM）和 CSV 引号/多行单元格；检查素材模板35列、缺口模板14列、非空且唯一的表头、记录宽度与两个规则文档非空。`*_fields.csv` 暂按实际CSV形状读取，不猜测哪一列代表“字段名”或“枚举”。未指定正式列名，所以同样列数的任意CSV通过形状检查也不代表符合正式契约。

返回六文件的字节 SHA256 和按文件名排序后的摘要集合指纹，版本由调用者明确提供。CLI只返回文件名、摘要和列数，不输出表头或模板内容。缺文件时退出码2、stderr返回 `missing_template` 与完整缺失文件列表；不建占位目录。格式错误、读取失败、路径越界分别记录错误码。摘要绑定所读取的字节，不提供源目录多文件事务快照，正式发布时仍须基于同一已校验版本冻结输入。

**仍待真实模板完成：**核对逐列顺序、字段说明表布局、锁定枚举及归属规则，补真实契约联调和版本摘要记录。当前 CLI 明确返回 `formal_semantics_verified=false`。测试中35/14列都是临时生成的 `synthetic_*`，不是正式表头。

## 建议、有效值与证据

`models.py` 使用严格类型、禁止额外字段和冻结对象；外部 JSON 通过 `model_validate_json` 进入。列表在JSON中表示，在Python模型内部使用 tuple。不要使用可绕过校验的 `model_construct` 或 `model_copy(update=...)` 导入外部输入。

| 结构 | 已实现边界 |
| --- | --- |
| `FieldValue` | `observed`必须有非空去重值；`unknown`、`no_match`、`not_applicable`、`failed`各自保留状态/原因且值为null，不能冒充false |
| `TagSuggestion` | 仅ai/tool建议；有实际值时需证据ID及产生者版本；没有人工确认字段 |
| `EffectiveValue` | 独立的人工字段修订，要求操作者ID、正整数revision、证据及带时区确认时间 |
| `Evidence` | 绑定源SHA256、证据对象/摘要；视频/音频片段要求有效时间区间，抽帧要求源时间戳 |
| `ArtifactRef` | 相对引用及SHA256；拒绝盘符、URL、路径穿越、Windows设备名等。真实根目录解析及文件哈希校验属于后续存储任务 |

这是结构和引用检查，不能证明真实推理已发生、人工身份真实、许可通过或医学标签准确。`WorkerResult`不能携带 `effective_values`；真正的人工值保护、逐字段角色检查与事务采用由D01/D03/D06实现。尚未发布的taxonomy不能从这些通用字符串字段绕过正式标签验证；正式标签接入必须经过后续词表/权限检查。

静态图片或文件名证据不自动成为动态方向或医学依据。D00保留证据类型与区间，未实现连续视频观察、医学审核或面向特定字段的证据充分性判断。

## 任务与结果

`TaskEnvelope`绑定素材ID/revision、task/job、attempt、lease_epoch、输入指纹及引用、版本集合、目标字段、配额和输出前缀；最多总尝试次数默认3，attempt不得超出预算，GPU槽只能0或1。输出前缀必须为 `jobs/<job_id>/runs/<task_id>/attempt-<attempt>/`。

`WorkerResult`区分 `succeeded/partial/failed/not_applicable`，保留耗时、可选峰值内存、产物、证据、建议及错误；失败/部分结果必须带错误，不适用必须说明原因，失败/不适用不能返回标签建议。证据ID不可重复，建议不得引用缺失证据。

`validate_result_for_task`检查结果的task/attempt/lease_epoch/输入指纹/版本一致、仅包含请求字段、产物位于输出前缀或完全匹配已登记输入。**它不提供运行时队列**，也不验证当前租约是否仍有效、输入文件真实哈希、取消状态、可见范围或素材revision是否已变化；这些由D01/D03等任务实现。输入指纹由后续调度者按内容/证据/规则/模型/词表等完整输入计算，D00不把不完整字段的摘要当成正式缓存键。

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

本轮关联R05/R10/R11、N03/N05，以及A06/A08/A09的D00结构前置。项目`.venv`中的60项测试通过；均为合成CSV/JSON或边界测试。真实表头/枚举、真实推理、许可、医学确认及服务端业务验收仍未执行，不能据此认定A/T/I整体验收通过。
