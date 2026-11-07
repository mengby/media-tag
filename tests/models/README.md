# D04模型验证

使用项目`.venv`和`requirements-vision.lock`，先运行`python -m media_tag prepare-models`。
`test_inference.py`和队列集成测试实际装载权重，要求Windows字体及兼容CUDA GPU；不以mock或skip冒充真实推理。
人脸阳性夹具保存在忽略目录`cache/models/d04/public-face-sample.jpg`：来源为
https://raw.githubusercontent.com/opencv/opencv/4.12.0/samples/data/lena.jpg ，
SHA256为`7de7ed51a1594fff247f4cae2301eceacf5313d6011e37b4a4c8733f7bb72c07`。
新环境需显式下载此公开诊断样本；不提交图片，不作为业务素材授权或人脸识别样本。

其余测试使用合成元数据、合成图片及明确的故障注入，检查权重/帧完整性、版本/GPU配额绑定、校准分组隔离和取消后子进程退出。
校准测试中的标签与判定仅为测试夹具，无医学含义；正式独立集和字段阈值尚未验收。
