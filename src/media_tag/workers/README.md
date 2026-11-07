# D02 技术提取与证据

`probe.py`负责外部工具检查、受预算约束的格式/技术信息、图像透明像素和音轨检测；`evidence.py`通过D01授权读取固定素材版本，在私有jobs目录保存输入副本、真实时间戳帧和阶段记录。D03队列/CPU执行适配见`../router/runtime.py`；D04实际模型/隔离执行适配见`../models/`；D10的`face_track.py`负责完整视频逐帧检测/跟踪，渲染和复核见`../transforms/`；当前没有常驻部署。

`media-tools.lock.json`仅记录当前Windows工具版本、来源及摘要，实际二进制和许可证保留在忽略的缓存目录。采样不等于连续观察，不证明无文字/无人脸或医学方向。完整说明见 [D02文档](../../../docs/media-evidence.md)。
