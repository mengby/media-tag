# 近似候选、实际媒体指纹与关系核对核心

2026-11-11。CT-06、CT-11、CT-16的独立核心实现。**尚未接入主库迁移、后台队列、人员页面、云发布或全库自动候选检索，也未完成真实业务校准。** 本轮没有下载/调用新模型，不自动合并资产、来源、owner、许可、标签或人工批准。

## 已实现能力

| 入口 | 实际行为 | 当前边界 |
| --- | --- | --- |
| `visual_bytes(data, source)` | 读取实际图片；EXIF方向规范化、透明像素以中性底合成、256位dHash、透明轮廓、低分辨率亮度和颜色特征 | 是视觉相似指纹，不是语义分类或医学识别 |
| 同上，动画图片 | 在预算内逐帧完整解码，保留帧序和原始时长信息；记录实际image/animation类型 | 不把GIF容器直接等同于实际动画；缺少帧时长保持未知，不编造运动方向 |
| `video_bytes(data, source, workdir, tools)` | 复用既有D02抽帧；使用实际解码PTS与源流起点，记录归一化样本时间 | 明确`sparse_decoded_video_frames`、`full_video_observed=false`；稀疏帧不能证明整片重复或动态过程 |
| `audio_bytes(data, source, workdir, tools)` | FFmpeg读取实际所选音轨，转16kHz单声道PCM，计算24个对数频带的归一化时序谱特征 | 使用现有vision可选依赖NumPy；没有依赖就明确失败，不用文件名、封面或转写代替声音 |
| `AuthorizedSimilarity.describe_clip` | 只复用已有当前CLIP模型报告中的实际向量；通过既有DraftIndex重新检查报告、帧与来源 | 不启动推理、不发布医学词表；取消、替换、损坏的当前生产者不能用旧向量回退 |
| `ranking_report` / `AuthorizedSimilarity.candidate_report` | 对显式候选池排序，区分信号不足、模型/描述符版本不兼容和源/生产者失效 | 没有已校准的自动判重阈值。`ranked`/`candidates`仅是取items的便利入口，编排应保留report诊断 |
| `RelationLedger` | 单独SQLite台账中登记候选，人工选择关系/方向，CAS修订、幂等回执及历史查询 | 关系核对不批准医学、隐私、入片用途或许可，也不修改主素材记录 |

视觉和音频描述符绑定稳定asset_id、asset_revision、实际内容SHA256、算法版本、解码器身份以及实际特征。CLIP还绑定模型/预处理版本、当前task_id、lease_epoch、报告及帧哈希；比较、台账查询和决定重试都会重新验证当前生产者。音频描述符包括所选流index、实际解码时长、可知的源起点和特征步长。

当前视觉/音频recipe及描述符schema均为v2：动画在当前帧load后取持续时间，保留浮点毫秒，不把分数时长截成整数。音频先对所选流执行`asetpts=PTS-STARTPTS`，保留原source_origin_seconds，并记录decoded_sample_count；实际解码时长必须与样本数/16000一致。解码到EOF，以输出字节和进程时间预算中止超限输入，不用输出`-t`截断后将前缀误报完整。音频特征时钟是实际PCM样本序列，不能当作已验证的容器事件时间映射。

旧v1产物可能包含延迟音轨截断或丢失/取整的动画时间，因此不作为当前可复用描述符/候选。历史候选JSON仍可识别其旧版本，但授权服务及关系核对会要求重新计算，不自动重用旧确认，也不删除或改写历史记录。

CLIP高相似可能只是主题相关；不能把其分数自动解释为重复画面、同一来源或同义医学标签。

源文件字节SHA256相同只产生`exact_bytes`事实候选。不同编码的视觉或音频可产生`visual_overlap`/`audio_overlap`候选；分数始终是`uncalibrated_similarity_not_probability`，calibrated、duplicate_confirmed、labels_copied和ownership_changed均为false。音频对齐偏移只是谱特征的估计，`temporal_alignment_verified=false`，不能直接用作裁剪或精确声音事件时间。

## 有界实际内容处理

- 原件只读；工具读取新私有目录中的校验副本。每次需提供新的、绝对本地workdir，其父目录应已存在；已存在目录不覆盖。
- 源字节默认上限128MiB，静态单帧上限16M像素，动画默认最多96帧、累计64M像素；超限明确返回预算错误，不把部分帧标为完整动画。
- 视频默认最多8帧，复用D02的600包/30秒采样范围，实际样本PTS保存在描述符中；不以采样范围冒充完整视频观察。
- 音频默认完整解码所选音轨，最多120秒；超限拒绝，不悄悄截前缀。多个音轨必须明确指定index。声学特征使用单声道混合，不宣称保留全部空间声道特性。
- PCM信号过弱时明确`insufficient_audio_signal`，不把不同静音文件当作可唯一辨识的录音；精确字节相同仍可由SHA256独立识别。
- NumPy仅为已有可选依赖，没有安装动作；代码和词表均未改动原35/14列契约。

## 授权调用与台账

`AuthorizedSimilarity(router, tools)`是目录数据的入口。`describe`使用当前FileService.open/read并在前后检查当前源版本；`candidates`只比较调用方明确提供且当前可读、文件门槛放行的候选。未授权/文件受限的池成员不返回身份、分数或隐藏数量。当前调用者可读但版本过期的描述符在report中保留缺失诊断。没有全库跨账号扫描接口。

低层`*_bytes`、`compare`只处理已授权的显式数据，**不是外部HTTP接口**。接入新的入口时必须从当前认证身份和FileService/Router已授权输入取得数据，不能接受客户端自报描述符为已验证证据。

台账初始化使用专用绝对本地目录，应用签名`0x4D545352`、schema版本1；遇到其他数据库会拒绝，并保持原文件不变。它不修改Catalog数据库迁移。主库备份/恢复、云发布及跨进程业务事务尚未整合，不能据此启用生产。

调用顺序：

1. 以当前SourceVersion生成描述符，或复用已验证当前CLIP描述符。
2. `candidate_report(query, explicit_pool, principal=...)`取得候选和缺失诊断。
3. `ledger.offer(candidate, request_id=..., principal=...)`登记待人工关系；当前人须能读两侧实际版本。
4. `ledger.decide(relation_id, RelationDecision(...), principal=...)`要求两侧当前edit权，明确same_recording、variant_of、related、different或uncertain；variant_of必须给方向。
5. `get/list/history`重验当前来源、文件门槛和CLIP生产者；失效后不回退旧确认。主库源/权限检查与独立台账之间采用前后复验，尚非跨库单事务。

request_id在台账内按actor限定；同输入重试返回原回执，改输入冲突。候选/决定/事件与回执在同一台账事务提交；实际测试了决定写入失败回滚。人工关系只表示这两份实际版本的关系判断；没有生成素材审核人、隐私批准、标签确认或内容转移。

## 实际验证与剩余条件

测试目录`tests/similarity/`使用合成PNG/APNG、实际MP4与非零起点视频、WAV/FLAC和多音轨容器，以及合成CLIP协议报告。覆盖透明隐藏RGB、缩放、实际PCM增益/重编码排序、静音不足、预算、源哈希、实际PTS、权限池过滤、当前生产者取消/篡改/新待办失效、独立台账CAS/幂等/回滚和外来数据库拒绝。

本分支首批验证85项关联检查及25项核心检查分别通过（有重叠）。跨审查随后实际复现延迟音轨旧结果1.536秒被误报完整；v2回归验证源时钟2秒的音轨保留全部32000样本/2秒。另验证无流时长的长音轨超限拒绝、WebP的80/120ms持续时间、分数APNG的1000/60及1000/120ms、v1失效和样本数一致性。最终执行`.venv/Scripts/python.exe -m pytest -q tests/similarity tests/files tests/catalog/test_catalog.py`共94项通过，其中包含32项相似核心检查；不与前批次相加。本分支没有自行提交或推送。

这些测试不是实际素材库近似判重精确率/召回率、正式阈值或CLIP模型质量验收；尤其不能宣称变速/变调、复杂背景声、长BGM和所有转码组合已鲁棒。CT-11的新语义粗分类、CT-14/15音频语义打标、全库索引/审核界面、自动编排、主库备份/云同步及独立业务对照仍待后续。
