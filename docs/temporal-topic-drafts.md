# CT-13：连续视觉十字段初稿核心

已实现独立的短视频、GIF、APNG、动画 WebP 连续视觉输入服务、Qwen3-VL-2B 视频接口适配、严格十字段解析和证据契约。默认模型执行关闭；本批没有调用真实权重/GPU，没有接入 Router/API/config/队列或修改目录有效值。工程协议通过不代表连续过程判断准确率、医学审核或业务验收完成。

## 实际输入与时间边界

视频读取明确选中的完整视频流；存在多个可用流时要求显式选择，不根据容器首个轨道猜测。以 `start_pts/duration_ts/time_base` 和逐帧 PTS 建立有理数时钟，保留非零源起点、源 PTS、源时间、归零时间与每帧时长。FFmpeg `fps_mode=passthrough` 解码选中流的**每一帧**，不 seek、不使用 fps 过滤或稀疏抽帧，解码数必须等于实际时钟帧数。拒绝缺失精确时钟、非递增 PTS、包时长冲突、动态尺寸、非方形像素、未经定义的透明视频和旋转规则。保留工具版本/摘要与实际显示 RGBA 摘要。

GIF/APNG/WebP 复用图片核心的完整逐帧解码，包括透明/合成/EXIF，保留精确有理延迟、循环次数与完整显示帧。APNG 默认海报不作为动画过程输入，其排除事实和 RGBA 摘要单独记录。动画只观察**一个完整周期**：无限循环不假装已经观察无限时长；有限多次播放也不把一次观察说成所有周期。静态图不进入本能力。

全部源帧按照帧时长的最大公约有理时间格展开：**只重复原画面以表达保持时长，不丢任何源帧、不调速**。例如 80/120 ms 两帧为 25 fps 的 `[0,0,1,1,1]`，1/60 和 1/30 秒为 60 fps 的 `[0,1,1]`。源帧与展开模型帧数分别记录，映射、总时长、FPS 和输入摘要必须一致。过细时钟产生过多模型帧时返回 `Pending('continuous_time_grid_requires_segmentation')`，不会降采样后冒充完整过程。

默认限制：原件 32 MiB、完整视觉流/周期 3 秒、源帧 48、模型时间格帧 64、单帧 1 M 像素、源总像素 16 M；可显式调整到契约内上限（6 秒、96 帧等）。常见 30 fps 的 3～5 秒素材可能超过预算，需要后续明确分段。**本版没有自动分段或任意裁剪功能**；过预算留待分段，不把首段或前 N 帧标成完整源。

实际送模型的每帧在白底合成透明，再按比例缩放并居中补白至 128×128（可显式选 256），保留原始显示帧和模型图各自摘要。高位深、ICC 转换等缺少明确策略的输入不静默改色。原件只读，完整原件快照、帧、连续清单和报告写入互不包含的私有工作区；不改名、不覆盖原件。

## 本机视频处理器接口核验

本批只读核验本机安装的 Transformers **4.57.6** 源码：

- `.venv/Lib/site-packages/transformers/models/qwen3_vl/processing_qwen3_vl.py`：`Qwen3VLProcessor.__call__(images=None, text=None, videos=None, **kwargs)`，视频支持 4D 数组；根据 `video_metadata.frames_indices/fps` 形成时间提示。
- 同目录 `video_processing_qwen3_vl.py` 与 `transformers/video_processing_utils.py`：`do_sample_frames=False` 可关闭采样；`do_resize=False` 保留服务已经声明和哈希的输入图尺寸。
- `transformers/video_utils.py`：`VideoMetadata` 的 `total_num_frames/fps/duration/frames_indices` 定义。

适配器通过 `apply_chat_template(..., tokenize=False)` 构造真正的视频占位文本，再调用 `processor(text=[...], videos=[ordered_4d_array], video_metadata=[...], do_sample_frames=False, do_resize=False, return_tensors='pt')`。它没有把帧当多张互不相关的图片。检查视频 grid 的时间格与完整帧数/尺寸，拒绝处理器再次丢帧或改变尺寸；模型十字段逐项生成，固定本地权重、无采样生成，禁止下载与远程代码。

Qwen 视频模型每两帧形成时间补丁；奇数帧会重复最后帧补齐，`processor_padding_frames` 显式记录，**不扩展源片段时长或宣称多观察一帧**。该 SDK 的文本时间提示是相邻帧中心并格式化到 0.1 秒；报告继续保留精确有理源时钟，不声称模型的时间文字具有同等精度。实际执行报告还记录安装处理器版本及四个相关源码文件的合成摘要。上述 API 核验与完全 mock 接线测试不等于已执行真实视频模型。

## 可调用接口

```python
from media_tag.temporal_topics import TemporalTopicService, LocalQwenVideoGenerator, Options, Pending
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain

generator = LocalQwenVideoGenerator(existing_semantic_model_root, enabled=False)
conditions = generator.readiness()  # 不读权重，不探测 GPU，不执行模型

# 后续协调独占 GPU、有界子进程/总时限后才可显式启用：
generator = LocalQwenVideoGenerator(existing_semantic_model_root, enabled=True)
service = TemporalTopicService(existing_private_workspace, policy=current_review_policy,
    generator=generator, tools=Toolchain(), options=Options(video_stream_index=None))
result = service.run(FilesystemBackend(original_directory), relative_key,
    expected_source_sha256=source_sha256, asset_id=asset_id,
    asset_revision=asset_revision, analysis_id=analysis_id,
    content_kind='video')  # 动画填 'animation'；动画不要求 tools
```

所有参数是可调用库接口，不是本轮已经运行真实模型的示例。`enabled=False` 拒绝执行，字符串 `'false'` 不能误启用。调用者须协调 GPU 租约和独立进程/硬超时；本独立服务不自行取得 GPU、创建 Worker 或对外服务。FFmpeg/ffprobe 各自有字节/时限边界；模型调用仍需后续有界 Worker 接线。不得与现有生产模型同时抢资源。

返回 `Result` 包含 `document: AnalysisDocument`、`sequence: Sequence`、`manifest/raw_report: ArtifactRef`。十字段严格为：用途大类、科室、器官/系统、疾病/症状、画面主体、过程/状态、表现形式、中文检索词、英文检索词、情绪/语气。`sequence` 验证所有源帧的完整顺序/时长、选中流 PTS 绑定、展开映射、输入摘要和处理器补齐；文档证据必须是绑定该清单的实际 `video_segment`，覆盖完整选中流或单个周期。

生成器输入只有有序模型图字节、精确网格 FPS/时长、输入摘要、问题与预算，**没有原文件名、来源、许可或原件路径**。原答逐字段 JSON，严格键/字段绑定、重复键、值类型、长度与状态规则；未知保留 unknown，协议坏记 failed，不补造值。闭合值逐字按当前已发布词表检查，缺失或不匹配保留原词候选与 no_match；文本检索短语按原表标量契约连接，不改表头或枚举。

动态方向/过程建议拥有实际连续段证据，仍是 AI 建议；医学四字段保留人工复核，所有建议均未校准、不能因自报信心豁免精标。本服务没有音频输入，报告固定 `audio_policy='not_analyzed'`，问题禁止推断旁白/BGM/声音/语音情绪；明确的听觉类答案退回 `unknown + evidence`，不能把视频有音轨当成已经听过。纯视觉不保证模型永不幻觉，实际正确率仍待真实评测和审核。

完整报告记录：原始输入 SHA、原件快照、实际连续清单/帧摘要、源时钟、输入网格、问题、原答、Qwen manifest revision、prompt/preprocess/policy/词表版本、处理器身份及候选。`synthetic_protocol` 与 `local_qwen_video` 明确区分；本批报告均来自合成协议测试。医学、隐私和有效值写入标志固定 false。后续入库/公开使用须继续经当前权限、FileService、输出隐私与用途检查，不能把私有原件或帧作为已批准预览。

## 验证与待办

实际检查命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/temporal_topics
.venv\Scripts\python.exe -m pytest -q tests/temporal_topics tests/local_topics tests/image_processing
```

仅临时目录合成 GIF/WebP/APNG、真实 FFmpeg 生成的短视频及完全 mock 的模型/处理器协议。覆盖完整帧/非零选中流 PTS、动画精确分数时序与海报排除、无丢帧网格、透明隐藏 RGB、源 hash/路径边界、报告绑定、严格 JSON、医学建议/缺词表、原件及人工值不变、拒绝音频推断、默认禁用与过预算待分段。真实权重/GPU 调用次数为零。

2026-11-11 实际验证：综合集 `tests/temporal_topics tests/local_topics tests/image_processing` **117 passed in 80.05s**；本包补充解码预算及选定模型绑定后，最终 `tests/temporal_topics` **37 passed in 30.85s**。测试有重叠，不累计计数。

仍待：明确分段策略、更多容器/多轨/VFR/旋转业务样本、资源调度及阶段接线、GPU 小批真实模型协议/内存/时限验证、连续过程准确率与独立校准、正式医学词表及人工验收、后续精标与完整端到端业务验收。工程核心不将上述条件标为完成。

父代理复核追加：高位深/未知像素格式、HDR与BT.2020视频先记待明确颜色转换策略，不直接降为8位RGB分析；实际合成yuv420p10le及8位PQ/BT.2020素材已验证在模型前等待。最后`tests/temporal_topics tests/image_processing` **71项通过（34.30秒）**，其中连续核心39项；只用实际合成媒体和mock模型，不计真实模型效果验收。私有证据`jobs/ct-temporal-core-20261111-94b8c120/validation.json`。
