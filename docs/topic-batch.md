# 首版批量初稿与实际进度

2026-11-12。本轮只补现有人员批量入口与状态接线，不启用生产、不选择或调用新模型，不增加全库近似召回、跨来源字段复用或连续视频阶段。

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

CSV继续原35列表头、AI初稿和人工优先规则；六非医学字段可使用合格建议，四医学字段无人工值时原CSV仍留空、建议仅质量侧表。没有旧模型registry也可按现有导出契约生成当前合格初稿；不等于模型效果验收或业务签收。

## 隔离验证

`tests/search/test_topic_batch.py` 使用真实合成PNG/WAV、实际CPUprobe/协议子进程及显式本地模型协议fixture，覆盖现代按钮能力、静态十字段批量/导出、音频和精标状态、缺模型终止待办、范围/owner隔离、损坏probe/静态报告修复与不相容版本不自动重跑。真实权重调用为零，不能据此声称生活场景、医学、贴纸、BGM或音效语义准确。

父代理复核：`tests/search/test_topic_batch.py tests/search/test_visual_fields.py tests/web/test_library.py` **39项通过（44.37秒）**；实际Chromium详情/批量状态与旧逻辑回归 **10项通过（1.99秒）**。前端识别实际`audio_segment`和`video_segment`证据类型。私有记录见`jobs/ct-mvp-closeout-20261112-60c7a19e/validation.json`；未启用生产或推送。
