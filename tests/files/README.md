# D17 文件基础接口测试

临时合成数据验证只读身份、逐对象/隐私链权限、固定哈希、Range/条件请求、
真实PNG/视频预览、源/缩略图损坏、断开关闭与实际loopback续传。无生产素材或医学确认。

`test_pcm_clock.py`覆盖真实单/双声道整数PCM WAV的精确样本时钟与实际audio_segment观察、44100Hz非有限小数时长、缺duration_ts旧报告及错误时基/多轨/未知格式/非整数PCM失败关闭。不会扫描PCM来补origin，也不把小数duration当样本数。
