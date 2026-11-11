# 可替换精标桥接与字段采用

2026-11-11。CT-17/18/19的部分工程实现：已接本地桥接协议、真实子进程执行、队列、字段汇总和修复。**当前只通过合成桥接验证，具体大模型尚未选择，没有真实视觉/音频模型能力或医学准确率验收。** 选择模型后仍须实现/验证对应analyze桥接及实际素材效果；不能只改model_id就宣布接通。

## 配置与调用入口

LocalConfig.precision默认enabled=false、auto_dispatch=false、providers为空。选定后由受信服务端配置provider_id、绝对本地adapter_path、代码SHA256、model_id、声明model_revision、支持的image/video/animation/audio输入及cpu/cuda设备，分别指定visual_provider和audio_provider。媒体、HTTP请求和模型返回均不能指定可执行代码或权限。

当前只实现execution=local的Python桥接，不包含外部HTTP/付费提供方。常用模型库配置为离线加载；这不是任意Python代码的网络沙箱。实际选型须审查受信桥接代码与运行环境。桥接代码哈希在执行端和子进程中复验后才编译已核验字节，代码大小上限4MiB；版本签名由代码、模型声明、设备与能力组成，不依赖本地代码地址。

隔离API已验证`POST /analysis/assets/{asset_id}/precision`：request_id、expected_asset_revision、source_sha256、expected_job_revision、fields（原十字段，默认全部）。复用现有认证/请求体上限/严格JSON检查，仅当前请求人自己的绑定分析意图、edit权限、当前文件门槛及未暂停/取消作业可以请求。模型未配或模态不支持时保持缺能力，不用视觉接口替代声音。

精标阶段加入已有分析作业，依赖probe，不依赖CLIP词表成功；processing快速任务独立。请求回执与重排在同一Router事务提交，确认丢失后同一ID不重复创建阶段，改输入须使用新ID。不同字段范围有独立阶段：较新的范围遮蔽该范围的旧结果，其余字段可保留合格来源，不用旧成功填新失败。

auto_dispatch须另外显式开启。它等待本地任务结束，只提交尚未请求精标的适用字段；已失败/未知/未匹配的精标不循环调用。切换模型不自动重跑已覆盖的整库，需明确请求/后续增量计划。扫描按真实凭证身份、有界候选和持久游标运行，重建Worker后受阻记录不会长期挡住后续候选；查询不触发模型或写扫描状态。

## 输入、时间与资源

v1桥接取得经过当前租约读取的**完整原编码副本**，原件只读；再次核验实际SHA256。图片不抹除透明信息，GIF不重编码丢循环，视频/动画/音频不以首帧、封面或转写文本代替输入。analyze(request)接收生成的私有输入路径、实际模态/时长、原十字段子集、已发布选项及固定提示版本，不接收原文件名、来源署名或账号凭证。

连续输入必须有可核对的单一所选流及有限流时长，按源起点归一化为0～stream span；不把非零PTS容器的结束时刻当时长。多流或缺少可验证时长保留待选择/证据。v1只支持整个有界输入，不支持声明局部时间却送整片。需要裁剪/更长BGM等应后续显式提供相应片段或容量策略，不自动套3～5秒或截短音频。

默认上限：128MiB输入、30秒连续内容、1MiB请求、512KiB回复、2MiB原生日志，均有配置/检查边界。超限、缺轨道/时长或配置缺失在字段视图区分为待能力、证据或人工，不冒充不适用；实际进程/协议失败仍是失败。配置的cpu/cuda对应Router资源请求，真实显存/吞吐须模型选定后另测。

子进程先等门闩，分配Windows Job Object后才加载桥接/模型。取消、超时和分配失败均清理进程树并等实际退出，才释放租约；超时预留清理/上报时间。桥接返回字段前后再次核验输入，返回内容不能替换输入或跨请求串用。

## 输出和字段规范化

桥接返回PrecisionReply：request_id、source_sha256、provider_revision及**恰好请求字段**。每字段有FieldValue、可选未匹配候选与动态标记。未知/无匹配保留；超出已发布选项的医学/用途候选转no_match并保留原候选，不自动扩枚举。没有可用词表时不能把猜测作为正式分类。纯音频两项视觉字段由系统记不适用，实际提供方只请求其余八项。

动态方向仍需video_segment；音频标签需audio_segment，静态图不能支持动态过程。模型回复多余批准/身份字段会被拒绝。server生成AnalysisDocument、实际whole-input证据及TagSuggestion，persist为AI建议；医学四字段仍逐项人工确认，人工有效值优先，许可/隐私批准由原服务负责。

报告记录实际耗时及可选ProviderUsage。令牌与费用只能记录提供方报告，缺报保持null，不能记作零费用；自报费用verified固定false。当前没有实际付费调用或账单核对。

## 资格、完成状态与修复

状态采用和修复共用verified：当前active且已释放尝试、实际报告/完整输入哈希、原始probe与真实流span、源/请求/模态、代码/模型签名、prompt/规则/词表版本、字段origin/versions/证据引用及建议一致性均须通过。失效立即撤下，不从旧成功回退。HTTP仅返回证据/报告摘要，私有路径不进入视图。

所有适用字段有可用初稿时可出现analysis_draft_ready/analysis_complete=true，**仅表示AI分析覆盖完成**，medical_confirmation、privacy_confirmation、effective_values_written仍false。未知/未匹配为analysis_needs_review或相应待办，不计覆盖完成；未经独立校准的本地免精标规则仍不启用。

同配置下损坏/失败精标可以通过[阶段修复](analysis-repair.md)重排。当前提供方、词表或规则已变时，旧规格进入awaiting_current_precision_configuration，不把旧policy_digest反复提交；应使用新precision请求明确选择当前配置，旧回执仅保留历史。配置更新不暗中覆盖人工值或重跑全库。

本轮测试是隔离真实CPU/原字节/进程树与合成标签协议，不能据此确认模型真实理解生活、医学、图表、贴纸、动作或声音。具体检查记录见[执行记录](cleaning-and-topic-tagging-execution.md)。Router/字段后端已接，完整页面、检索/CSV/发布投影和CT-23/24业务验收仍待后续；未部署、重启或操作生产实例。
