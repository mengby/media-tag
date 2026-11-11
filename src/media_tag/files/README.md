# D17 文件基础接口

`service.py` 统一对象权限/隐私链检查、固定哈希、后端验证、预览资源和清单；
`http.py` 实现条件请求、单区间 Range、流式关闭和安全文件名。不接受任意路径/URL。
实现边界见 [D17 文件说明](../../../docs/file-access.md)。

D17公共检索、能力发现及受限播放区间见 [API说明](../../../docs/asset-search-api.md)；models.py 校验实际所选文件的播放范围。

`clock.py::pcm_wav_sample_clock(data)`仅从真实probe元数据建立单轨整数PCM WAV样本索引时钟：format=wav、精确time_base=1/sample_rate、duration_ts>=1，缺PTS且start_pts缺失/0。返回明确origin_basis、sample_count/rate与duration_rational，保持current_file_normalized_seconds接口；不扫描PCM，不用小数duration推样本数，不为多轨/视频/未知格式补origin。缺精确字段的旧报告保持未知。

probe只补可选duration_ts/start_pts元数据，不升级全局PROBE_VERSION或CPU预处理规则，不触发既有图片/模型重排。旧报告的显式补探测/缓存刷新、旧upload.validation_json优先级另列待办；本轮不回写旧报告或生产库。
