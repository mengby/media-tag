# 图片与动态贴纸区域打码核心

2026-11-11，CT-09局部工程实现，CT-10仅提供明确遮挡与质量边界。代码位于`src/media_tag/image_processing/`，已在隔离合成素材上验证，**未接生产实例、上传/Router/VariantService登记或人工审核页面，也不代表自动人脸检测/医学/隐私业务验收完成**。

## 已实现的可调用核心

`ImageProcessor(workspace).process(source_backend, source_key, expected_source_sha256=..., job_id=..., recipe=ImageRecipe(...))`从显式只读`FilesystemBackend`读取调用方已授权的源文件。源与输出目录禁止重叠，源SHA256和读取期间文件状态必须匹配。库不扫描目录，不转移owner，不修改、重命名或覆盖原件。

配方必须明确`output_format`为png或apng，并给出一个或多个区域。每区明确purpose(face/watermark/other)、normalized_display_after_exif坐标、solid/mosaic/blur方法，可选padding、强度和包含首尾的frame_start/frame_end。没有通用“自动去水印”开关，没有生成式修复或凭画面推断许可。新格式枚举仅属于处理配方，不修改原35/14列表头或锁定选项。

坐标复用原视频处理的pixel_box规则，作用于EXIF方向纠正后的实际显示画布。RGB处理仅改变区域内像素，原alpha逐像素保留；mask外的标准化RGBA不变。静态8位PNG/JPEG/GIF/WebP/BMP/TIFF可转换为PNG。源RGB ICC配置保留，EXIF元数据不复制。高位深、CMYK等需要另选颜色转换策略的输入会拒绝，不暗中降低医学灰阶精度。

## 动画与透明保护

GIF、APNG和动画WebP按解码后的完整合成帧逐帧处理。动态图必须显式选择apng，不能以png导出首帧冒充整段处理。APNG默认海报也作为单独显示帧处理。涉及人脸区域时，每个显示帧必须有对应区域或绑定该源帧像素的显式“不请求人脸遮挡”声明；仅提供首帧区域、其余帧未明确处理会拒绝。区域可按帧变化，但本模块不自动插值或宣称已检测全部人脸。

### 仅部分帧需要遮挡时

先调用只读`processor.inspect(source_backend, source_key, expected_source_sha256=...)`，取得每帧EXIF规范化后的`source_rgba_sha256`、帧索引、角色和时序。它复用处理时相同的路径/源哈希/字节/像素/帧预算检查，不创建输出文件，不进行人脸检测或批准。调用方仍须先走当前FileService授权和源版本检查，不从接口取得新的权限。

对确实未请求人脸遮挡的显示帧，可给`ImageRecipe.unmasked_frame_declarations`添加`UnmaskedFrameDeclaration(frame_index=1, source_rgba_sha256=inspection.frames[1].source_rgba_sha256, reason='no_face_mask_requested')`。该帧的hash与实际解码源帧必须一致，索引必须存在且唯一，并不能同时有face区域；缺少声明、错误hash、重复、越界或冲突都会拒绝。默认海报同样可以用其独立索引/hash声明。

这是调用方明确的处理请求，**不表示检测无脸、不伪造人工观察人或隐私批准**。结果逐帧保存源RGBA摘要和完整声明；未有其他操作的该帧像素不变，全片privacy/quality仍为pending。最终输出仍需绑定实际输出哈希进行完整隐私复核。

输出使用受控的完整画布RGBA APNG：保留帧数、精确有理数帧延迟、有限/无限播放次数和独立默认海报。不做帧去重，完全相同的打码后帧也不合并；不强套3～5秒。输出重新解码并逐帧比较RGBA、alpha、时序、循环和海报结构。

播放次数转换按格式语义保存：APNG num_plays是总播放次数，0为无限；GIF正loop值表示初次之后的重复次数，因此转为loop+1，缺失loop为播放一次；动画WebP保留总播放次数。[W3C PNG规范](https://www.w3.org/TR/png/)及[Chromium WebP解码实现说明](https://chromium.googlesource.com/chromium/src/+/42b474cc4899601a878d04b6c9245737b81cf231/third_party/blink/renderer/platform/image-decoders/webp/webp_image_decoder.cc)支持这些语义。

零/缺失帧延迟、不能精确编码的延迟、多页非动画、超过输入/输出/像素/帧预算等会明确失败，不能用猜测时长或静态副本掩盖。仍需真实业务动画播放器和代表性样本验收；本轮不把这些工程检查计作CT-09整体验收。

## 输出、复核与水印边界

输出位于调用方私有workspace的`jobs/<job_id>/image-processing/`；稳定文件名绑定源哈希与配方/渲染版本摘要，原文件名不改。结果记录完整配方及其摘要、源/实际输出SHA256、逐帧RGBA/alpha摘要、区域、帧延迟、循环、方向及RGB配置摘要。已存在的输出或报告不会覆盖，调用层需先验证既有结果再决定复用/恢复。

所有结果固定`privacy_status=pending`、`quality_status=pending`，检测/隐私/质量/许可确认及effective_values_written均为false。处理成功不等于人脸检测完备，也不等于所有真实身份细节已经不可辨识；alpha与轮廓是否合适仍要复核。未复核文件只能经原FileService的私有复核通道使用，不能直接加到已处理公开入口。

watermark区域可执行明确遮挡；`watermark_quality_status=pending`与隐私状态分别保存，`watermark_restoration_performed=false`。遮挡不恢复水印下内容，不证明文本、医学结构或许可正确；具体去水印方法、质量审核与许可记录仍待CT-10/20，不自动批准。

## 验证和后续整合

`tests/image_processing/`使用实际合成RGBA/JPEG/GIF/APNG/WebP字节，验证区域像素/alpha、方向与ICC、精确1/60秒帧延迟、相同帧不合并、默认海报、循环映射、逐帧覆盖、显式未遮挡声明及只读检查器、只读哈希、路径/覆盖保护和处理预算。没有运行新模型、真实人员素材或正式医学样本。

后续须由主流程接入实际检测候选、受权文件读取、输出衍生登记、人工值/隐私与质量审核及真实业务播放器验收。底层处理器只接受调用方明确提供的本地只读源，不能替代FileService的对象权限与当前输出哈希复核。
