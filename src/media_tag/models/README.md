# D04 模型公共实现

`registry.py` 负责本地权重/配置摘要与显式下载；`inference.py` 提供实际 OCR、YuNet、
Chinese-CLIP 原语；`runner.py` 是受限子进程入口，`calibration.py` 记录字段校准和拒识。
`resident.py` 为本机长期 Worker 提供可选的 CUDA CLIP 常驻子进程，每个租约仍独立推理与校验。
权重、图像、向量、日志不提交 Git。运行边界见 [D04说明](../../../docs/model-workers.md)。
