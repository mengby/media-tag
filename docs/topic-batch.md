# 首版批量初稿与实际进度

2026-11-12。本轮只补现有人员批量入口与状态接线，不启用生产、不选择或调用新模型，不增加全库近似召回、跨来源字段复用或连续视频阶段。

## 可配置打标字段

本机服务启动时读取 [tag-fields.json](../src/media_tag/analysis/tag-fields.json)。默认第一项是“口语化表达”，用于生成与实际画面或原声有关的口播搜索短句。原话使用记录仍写入“曾用原话”。每个扩展字段是一个文本值，支持一至多句。名称同时用于模型请求、详情、检索和 CSV 表头。要再加一列，可在 `fields` 数组末尾加入这样的对象：

```json
{"name": "动作描述", "prompt": "用自然中文描述素材中有证据的动作。静态图片仅描述能看见的姿态，证据不足时返回 unknown。"}
```

可以在本机运行配置的 `review.tag_fields_file` 指向自己的绝对路径。省略该项使用随包配置，显式设为 `null` 或配置空数组可以只使用原字段。API 和 Worker 读取同一份配置文件，保存文件后，下一次打标、查询或导出会读取更新。只有改换运行配置中的文件路径才需要重载服务。原模板的字段名、重复字段名和危险 CSV 表头会被拒绝。单份配置支持最多 32 个附加文本字段。新增字段使用独立任务，已有字段的模型规则摘要只绑定自己的提示。

- 新入库的静态图片（含经探测的单帧 GIF）可由已启用的本机 Qwen 自动打扩展字段。需要指定精标模型处理这一列时，使用 `POST /analysis/assets/{asset_id}/precision`，按[精标配置及请求契约](precision-adapters.md)提交源摘要、修订号和 `"fields": ["口语化表达"]`。提供方必须支持所选媒体类型。精标自动派发沿用 `precision.auto_dispatch`。第一次自动精标包含当时配置的扩展字段，已经完成精标的素材由显式补打入口补新列。外部提供方接收实际媒体及所请求的提示词。
- 批次补打可从仓库根目录运行 `.\.venv\Scripts\python.exe -m media_tag intake backfill --batch <已导入批次目录> --local-root D:\ai\media-tag\data\local`，这条命令返回排队结果，每项含素材 ID 和所请求的字段。模型完成后，用同样的批次和本机参数运行 `intake drafts` 导出当前快照。
- 程序调用 `POST /search/auto-tag/backfill`，用 `assets` 显式指定素材、源 SHA256 和当前修订号。人员页的“仅补我的素材缺少的扩展字段”作用于本人素材库（上限 500 条）。只补没有任务和人工值的扩展字段，已排队、已拒绝或已尝试的字段保留其状态。已知失败或证据损坏可通过相应阶段修复。返回待配置、待作业或素材状态变化时，按该状态处理。
- HTTP 或 MCP 查询带 `include_drafts: true` 可召回已验证的口语化短句。导出 `assets.csv` 默认将新列按配置顺序追加在原 35 列之后；空值保留为空。使用 `include_ai_drafts: true` 才写出尚未确认的建议。只需原版 35 列时可在 `POST /review/exports` 设置 `include_configured_fields: false`。导出清单中的 `asset_columns` 是该快照的实际列数。

## 实际入口与范围

继续使用 `POST /search/auto-tag`、`/search/auto-tag/status`、`/search/auto-tag/export` 及原 `assets` 参数。显式选择按当前actor检查owner、edit权限、源SHA256和FileService门槛；空选择仍表示该actor明确提交其当前有效素材库，最多500条。不同owner的同字节记录不合并，不按数据库owner创建actor。

现代模式复用 `AnalysisService` 的真实部署配置，在每个所选资产上登记分析意图，按原 `intent_id` / actor 的Router绑定提交或复用作业。这里不调用会扫描actor其他意图的后台派发方法。未知类型只先实际probe，随后通过原采用函数增加缺失阶段；不能由文件名、封面或上传扩展名推断声音/画面类型。

再次点击只对当前同规格绑定作业做基础补缺/损坏或失败重试，并保留有效阶段。模型、规则或预处理配置不相容时返回 `awaiting_explicit_version_repair`，需另外明确调用原修复入口，不因按钮或配置变化重跑整个已选批次。暂停/取消不自动恢复。人工拒绝后的自动新建议仍未接通；已有人工值、unknown、拒绝与医学/隐私门槛继续有效。

新静态十字段、本地音频或精标阶段存在，或分析已启用时使用现代路径；缺少旧 `semantic_registry` 不再造成503。旧四字段专用配置保留原接口、完成判断和回执兼容。人员会话的按钮能力同时识别新阶段或已启用的分析服务，能力提示不授予角色或对象权限。

## 执行与语义状态分别记录

现代状态保留 `total/completed/running/failed/not_requested` 等字段，并新增：

| 字段 | 实际含义 |
| --- | --- |
| `execution_complete` | 当前没有可继续运行的任务；失败、暂停、取消或缺能力也可终止轮询 |
| `completed` | 已结束且没有执行失败的资产数，可能仍有待模型/复核字段 |
| `semantic_complete` | 原十字段投影的后续动作均为none；不代表人工、医学、隐私或许可批准 |
| `waiting` | 执行已结束但语义尚未覆盖的资产数 |
| `pending_capability` | 缺少当前分析/精标能力的资产数 |
| `pending_fields` | 仍有后续动作的字段数；类型尚未取得时保留十字段待办 |
| `review_required` | 含待字段审核的资产数 |
| `items` | 仅所选资产的执行/语义/能力摘要，无私有产物路径 |

`state=ready` 仅表示当前执行已终止且可以导出合格的现有AI初稿；unknown/no_match/缺模型仍明确保留，不能称十字段已完成。执行失败为partial且 `execution_complete=true`，不无限轮询、不自动导出。模型未配置时不会制造任务成功或标签，关闭分析时只登记意图并显示待配置。已配置但未开启自动后续派发的提供方仍需原显式请求；批量按钮不暗中改变部署开关。

状态采用原十字段共享验真/当前字段范围遮蔽规则，执行任务同时纳入 `local_topics/audio_topics/precision`。素材库状态同样识别这些阶段，只有通过原投影资格的建议才显示待审核，probe/evidence成功仍只表示技术就绪。已确认的人工作用值保持优先。

CSV 保留原 35 列及其顺序，再追加已配置的扩展列。已有人工作用值优先。原六个非医学字段及新增文本字段可使用合格建议。四个医学字段尚无人工值时在主表中留空，建议见质量侧表。没有旧模型registry也可按现有导出契约生成当前合格初稿；不等于模型效果验收或业务签收。

现代批量导出使用独立请求ID，由原ExportService在一次完整快照校验后按内容fingerprint去重：相同快照复用原导出，十字段报告失效/修复或当前字段范围更新后可产生新导出，不以旧registry/tag_suggestions的局部摘要复用变更请求；旧四字段请求键保持兼容。

## 隔离验证

`tests/search/test_topic_batch.py` 使用真实合成PNG/WAV、实际CPUprobe/协议子进程及显式本地模型协议fixture，覆盖现代按钮能力、静态十字段批量/导出、音频和精标状态、缺模型终止待办、范围/owner隔离、损坏probe/静态报告修复与不相容版本不自动重跑。真实权重调用为零，不能据此声称生活场景、医学、贴纸、BGM或音效语义准确。

另覆盖真实POST选择全新WAV后由配置Worker执行CPU probe，再按 `audio_topics.enabled=true`、`auto_dispatch=false/true` 分流：关闭时无音频任务、明确待显式请求且重复轮询不派发，显式HTTP仍可执行；开启时自动生成并采用实际音频协议报告。此为合成桥接端到端接线验证，未运行真实声音模型。

父代理复核：`tests/search/test_topic_batch.py tests/search/test_visual_fields.py tests/web/test_library.py` **39项通过（44.37秒）**；实际Chromium详情/批量状态与旧逻辑回归 **10项通过（1.99秒）**。前端识别实际`audio_segment`和`video_segment`证据类型。私有记录见`jobs/ct-mvp-closeout-20261112-60c7a19e/validation.json`；未启用生产或推送。

追加音频开关与导出更新用例后，批量/旧四字段/音频配置/本地派发/素材库关联 **48项通过（63.10秒）**；父代理对analysis、本地十字段、音频与精标共同状态回归 **166项通过（127.83秒）**。本地或音频已通过共享验真但字段未齐时，摘要为`analysis_needs_review`，不再误报缺音频提供方；逐字段后续动作仍保留，不能据此算语义全项完成。批次有重叠，不相加。
