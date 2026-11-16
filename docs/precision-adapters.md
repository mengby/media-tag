# 可替换精标桥接与字段采用

精标 Worker 支持 Codex CLI 图片精标、本地 Python 桥接和外部多模态 API。三种方式共用队列、证据校验与字段审核流程。

## 配置与调用入口

本机配置在 `data/local/runtime.local.json` 的 `precision` 段。默认 `enabled=false`、`auto_dispatch=false`、`providers=[]`。视觉和音频可用 `visual_provider`、`audio_provider` 分别选择。

### Codex CLI 图片精标

将[图片精标配置示例](../config/precision-codex.example.json)中的设置合并到本机运行配置。已有其他提供方时，将 `codex-terra` 追加到 `precision.providers` 并选作 `visual_provider`。API 和 Worker 应加载相同配置，开启 `analysis.enabled`、`review.enabled` 和 `precision.enabled`。示例保留手动派发，从现有精标入口请求选定图片与字段。

Worker 使用运行账户已登录的原生 Codex CLI，通过 `codex exec --model gpt-6.1-terra --image … --output-schema …` 传入图片。当前实现需要支持 `--ignore-user-config`、`--ephemeral`、`--output-schema` 的 CLI，本机实测版本为 `0.167.0-alpha.3.2`。在 Worker 账户终端用 `codex login status` 检查登录。如果登录任务的 PATH 找不到 CLI，在该提供方的 `cli_path` 填入 PowerShell `(Get-Command codex.exe).Source` 返回的绝对路径，客户端更新后若路径变动需同步。此方式沿用 CLI 登录凭证。[OpenAI 官方模型说明](https://learn.chatgpt.com/docs/models) · [非交互调用](https://learn.chatgpt.com/docs/non-interactive-mode)

支持静态 PNG、JPEG、WebP 和单帧 GIF。Worker 校验原图哈希和静态格式，再复核本次外发权限；CLI 在独立临时目录接收匿名附件和所选字段的提示词、类型与词表。返回的字段进入原精标建议与人工复核流程。子进程退出、超时或取消后回收临时文件。CLI 缺失报 `precision_codex_missing`；登录、模型权限或 CLI 执行异常导致非零退出时记为 `precision_codex_process`。

2026-11-16 在隔离库通过真实 Worker 和本机已登录的 CLI 验证了十个基础字段。测试使用新生成的白底红圆 PNG，模型返回全部十个字段，观测值包含红色及圆形，报告源哈希与附件一致，未写入人工有效值。业务图片准确率与医学字段仍需分别复核。

### 外部模型 API

将下面的 `precision` 合并到现有运行配置，替换 `base_url` 和 `model_id`，在启动本机 Worker 的账户环境中设置 `MEDIA_TAG_PRECISION_API_KEY`。API 与 Worker 需加载相同的模型配置。准备好后确认 `analysis.enabled` 和 `review.enabled` 已启用，将 `precision.enabled` 改为 `true`，让服务重新加载配置，通过原精标入口选择一份允许发往该提供方的素材验证。

```json
{
  "precision": {
    "enabled": false,
    "auto_dispatch": false,
    "visual_provider": "external-vision",
    "providers": [{
      "provider_id": "external-vision",
      "execution": "chat_completions",
      "base_url": "https://your-provider.example/v1",
      "model_id": "your-model-name",
      "api_key_env": "MEDIA_TAG_PRECISION_API_KEY",
      "inputs": ["image"]
    }]
  }
}
```

- 协议为 Chat Completions。可填 HTTPS 基地址（自动追加 `/chat/completions`）或完整接口地址。Key 通过 `Authorization: Bearer` 发送，仅传给本次远程调用的子进程。地址不接受 URL 凭证、查询参数或重定向。仅本机回环 IP 支持 HTTP。
- `inputs` 声明允许外发的素材类型。图片使用 `image_url`，支持 PNG、JPEG、WebP 和静态 GIF；MP4 视频需要提供方支持完整内嵌 `video_url`；WAV/MP3 音频需要 `input_audio`。使用音频时把 `audio_provider` 指向声明 `audio` 的提供方。当前外部适配器不接动态贴纸。示例仅开启图片能力。协议参考 [Chat Completions](https://developers.openai.com/api/reference/resources/chat) 和[视频输入扩展](https://www.alibabacloud.com/help/en/model-studio/vision)。
- 每次调用会将**完整素材字节**、请求标识、请求字段、字段类型、词表选项和证据时间范围发往所填地址。正常精标请求和受权修复会为当前任务生成授权记录。Worker 在发送前复查这条记录、当前租约、编辑权和文件复核状态。开启自动派发后，符合条件的素材也会发往该提供方并可能计费。
- `model_revision` 可填写供应商模型快照版本。换地址、模型或声明版本会生成新的精标签名。轮换 Key 不影响已有结果。生产者用量仅记录提供方返回的 token 数。鉴权失败、限流、非成功 HTTP 状态、超时、格式不支持和无效回复均进入失败流程。

### 本地 Python 桥接

租用的 Qwen3-VL-8B-Instruct 可以通过 SSH 转发的回环端口接入。该图片提供方可设置
`"image_reply_format":"compact"`，一次请求全部已配置字段，文本检索词数组在写入单元格时以分号连接。
`precision.image_workers` 设置桌面 Worker 的独立图片请求数（0 至 8）；每张图独立持有租约并复查外发权限。
本地前处理和线上发布可与远端推理同时进行。开启自动派发时，可将 `auto_dispatch_after_intent` 设为启用前最后一个
`analysis_intents.rowid`，限定到后续新入库的分析意图。已有素材仍可显式请求精标。

`execution=local` 配置绝对 `adapter_path`、代码 SHA256、`model_id`、`model_revision`、支持的输入和 CPU/CUDA 设备。执行端和子进程复验代码哈希后才加载，代码大小上限 4 MiB。

隔离API已验证`POST /analysis/assets/{asset_id}/precision`：request_id、expected_asset_revision、source_sha256、expected_job_revision、fields（原十字段，默认全部）。复用现有认证/请求体上限/严格JSON检查，仅当前请求人自己的绑定分析意图、edit权限、当前文件门槛及未暂停/取消作业可以请求。模型未配或模态不支持时保持缺能力，不用视觉接口替代声音。

精标阶段加入已有分析作业，依赖probe，不依赖CLIP词表成功；processing快速任务独立。请求回执与重排在同一Router事务提交，确认丢失后同一ID不重复创建阶段，改输入须使用新ID。不同字段范围有独立阶段：较新的范围遮蔽该范围的旧结果，其余字段可保留合格来源，不用旧成功填新失败。

auto_dispatch须另外显式开启。它等待本地任务结束，只提交尚未请求精标的适用字段；已失败/未知/未匹配的精标不循环调用。切换模型不自动重跑已覆盖的整库，需明确请求/后续增量计划。扫描按真实凭证身份、有界候选和持久游标运行，重建Worker后受阻记录不会长期挡住后续候选；查询不触发模型或写扫描状态。

## 输入、时间与资源

v1桥接取得经过当前租约读取的**完整原编码副本**，原件只读；再次核验实际SHA256。图片不抹除透明信息，GIF不重编码丢循环，视频/动画/音频不以首帧、封面或转写文本代替输入。analyze(request)接收生成的私有输入路径、实际模态/时长、原十字段子集、已发布选项及固定提示版本，不接收原文件名、来源署名或账号凭证。

连续输入必须有可核对的单一所选视觉/音频流及有限时长，并核对全部实际音视频流的公共起点和终点。桥接接收完整输入，证据覆盖归一化后的整份输入范围；非零PTS且仅视频从5秒开始的0.4秒素材仍为0～0.4秒，而音频从0秒、视频从5秒开始的组合覆盖0～5.4秒，不能误用0～0.4秒批准后面的画面。单轨整数PCM WAV可用新探测的精确样本数/时基建立零点；缺起点、缺精确时钟或多条目标轨保留待证据/选择。请求显式给出共同时间基准与evidence_start_seconds/end_seconds；声明局部时间却送整片不受支持。需要裁剪/更长BGM等应后续显式提供相应片段或容量策略，不自动套3～5秒或截短音频。

默认上限：128MiB输入、30秒连续内容、1MiB Worker 请求元数据、512KiB 回复、2MiB原生日志，均有配置/检查边界。外部 HTTP 请求另含完整素材的 Base64 编码，编码大小为原始字节数向上取整到 3 的倍数后乘以 4/3。超限、缺轨道/时长或配置缺失在字段视图区分为待能力、证据或人工，不冒充不适用；实际进程/协议失败仍是失败。配置的cpu/cuda对应Router资源请求，真实显存/吞吐须模型选定后另测。

子进程先等门闩，分配Windows Job Object后才加载桥接/模型。取消、超时和分配失败均清理进程树并等实际退出，才释放租约；超时预留清理/上报时间。桥接返回字段前后再次核验输入，返回内容不能替换输入或跨请求串用。

## 输出和字段规范化

桥接返回PrecisionReply：request_id、source_sha256、provider_revision及**恰好请求字段**。每字段有FieldValue、可选未匹配候选与动态标记。未知/无匹配保留；超出已发布选项的医学/用途候选转no_match并保留原候选，不自动扩枚举。没有可用词表时不能把猜测作为正式分类。纯音频两项视觉字段由系统记不适用，实际提供方只请求其余八项。

动态方向仍需video_segment；音频标签需audio_segment，静态图不能支持动态过程。模型回复多余批准/身份字段会被拒绝。server生成AnalysisDocument、实际whole-input证据及TagSuggestion，persist为AI建议；医学四字段仍逐项人工确认，人工有效值优先，许可/隐私批准由原服务负责。

报告记录实际耗时及可选ProviderUsage。令牌与费用只能记录提供方报告，缺报保持null，不能记作零费用；自报费用verified固定false。当前没有实际付费调用或账单核对。

## 资格、完成状态与修复

状态采用和修复共用verified：当前active且已释放尝试、实际报告/完整输入哈希、原始probe与实际公共时间范围、源/请求/模态、代码/模型签名、prompt/规则/词表版本、字段origin/versions/证据引用及建议一致性均须通过。失效立即撤下，不从旧成功回退。HTTP仅返回证据/报告摘要，私有路径不进入视图。

所有适用字段有可用初稿时可出现analysis_draft_ready/analysis_complete=true，**仅表示AI分析覆盖完成**，medical_confirmation、privacy_confirmation、effective_values_written仍false。未知/未匹配为analysis_needs_review或相应待办，不计覆盖完成；未经独立校准的本地免精标规则仍不启用。

同配置下损坏/失败精标可以通过[阶段修复](analysis-repair.md)重排。当前提供方、词表或规则已变时，旧规格进入awaiting_current_precision_configuration，不把旧policy_digest反复提交；应使用新precision请求明确选择当前配置，旧回执仅保留历史。配置更新不暗中覆盖人工值或重跑全库。

本轮测试是隔离真实CPU/原字节/进程树与合成标签协议，不能据此确认模型真实理解生活、医学、图表、贴纸、动作或声音。具体检查记录见[执行记录](cleaning-and-topic-tagging-execution.md)。Router/字段后端已接，完整页面、检索/CSV/发布投影和CT-23/24业务验收仍待后续；未部署、重启或操作生产实例。
