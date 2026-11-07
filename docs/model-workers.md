# D04 OCR、人脸与Chinese-CLIP Worker

2026-11-07：本机工程实现与真实推理验证完成。源代码位于`src/media_tag/models/`，测试位于`tests/models/`。仅登记观察证据、帧内检测和表示向量；正式医学词表、代表性独立集、标签接受率、全片人脸轨迹/打码仍待D13/D06/D07/D10。

## 固定模型与环境

| 能力 | 固定版本 | 实测执行位置 |
| --- | --- | --- |
| OCR | RapidOCR 3.9.2；PP-OCRv5 mobile检测/识别＋PP-LCNet方向；转换版v3.9.2 | ONNX Runtime 1.23.2 CPU，单线程 |
| 人脸 | YuNet 2023mar；opencv_zoo revision `d4938dfc9d4ec5d098bfa33e98b3f3345a236586` | OpenCV 4.12.0.88 CPU，单线程 |
| 图文表示 | OFA-Sys Chinese-CLIP ViT-B/16；revision `36e679e65c2a2fead755ae21162091293ad37834` | PyTorch 2.9.1+cu128，Transformers 4.57.6；CUDA默认，CPU须显式配置 |

`models.lock.json`固定11份权重/配置/词典/上游说明或许可证的来源、字节数及SHA256，整个清单的摘要作为`model_revision`。实际权重位于忽略目录`cache/models/d04/`，不进入wheel或Git。加载前完整校验；文件缺失/变化明确失败，不静默换模型、不在推理期间自动下载。CLIP仅用本地配置、`weights_only=True`读取固定state_dict，不运行远端代码。

本机Python 3.12.14，RTX 5060 Ti 8151MiB，驱动580.97；驱动宣告最高CUDA 13.0，实际torch运行时为12.8，包含`sm_120`。实际CUDA矩阵运算与中文图文编码均通过；不是仅凭`nvidia-smi`判断。用户目标RTX 5060及最终服务器仍须D07实测。

Windows系统VC++ DLL为14.30，初次ONNX导入发生WinError1114。`msvc-runtime==14.51.36231`把运行库装入项目`.venv/Scripts`；`native.py`在导入模型依赖前显式加载本环境的MSVC DLL，已验证ORT与torch可共存。未更改系统运行库、显卡驱动或相邻项目。直接绕过适配器导入ORT不属于已验证入口。

Transformers 4.57.6的Chinese-CLIP文本入口读取不存在的pooler；适配器按[官方Chinese-CLIP实现](https://github.com/OFA-Sys/Chinese-CLIP/blob/master/cn_clip/clip/model.py)取最后层CLS并经过`text_projection`。测试实际比较中文文本的CPU/CUDA结果，未修改第三方安装包。

## 安装与运行

核心服务仍可只安装27包`requirements-dev.lock`；视觉环境完整锁为61包`requirements-vision.lock`。两者固定的核心版本一致；本轮依用户要求在项目`.venv`安装视觉依赖。

已有完整wheel缓存的离线安装命令已验证：

```powershell
& .\.venv\Scripts\python.exe -m pip install --no-index --find-links cache/d00-wheels --require-hashes --only-binary=:all: -r requirements-vision.lock
& .\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m media_tag prepare-models
& .\.venv\Scripts\python.exe -m media_tag check-models
& .\.venv\Scripts\python.exe -m pytest -q
```

新环境没有缓存时，拟定在线安装将第一条改为`pip install --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cu128 --require-hashes --only-binary=:all: -r requirements-vision.lock`。Torch wheel约2.86GB、CLIP权重约753MB，需预留下载与解包空间。此次国内镜像下载的PyPI wheel逐一对照官方PyPI发布摘要；torch对照官方cu128索引摘要。首次RapidOCR/PyTorch下载曾超时，后以镜像/断点分块完成，最终文件全量哈希一致。缓存不是部署备份。

`prepare-models`是显式联网管理命令，按清单写私有缓存；失败的临时文件不安装，已有不同字节不覆盖。新机器测试前还需显式取得[测试说明](../tests/models/README.md)中的固定OpenCV公开人脸诊断图；该图片不随软件发布。

在既有本地配置增加以下字段后，`serve`与`work-once`可执行模型阶段（目录须改为本机绝对路径）：

```json
"models": {
  "enabled": true,
  "model_root": "D:/ai/media-tag/cache/models/d04",
  "clip_device": "cuda"
}
```

默认`enabled=false`，文件服务不要求安装/装载模型。显式选择`clip_device="cpu"`时GPU配额为0；CUDA不可用则失败，不自动转CPU。配置仍需D03规定的凭证、模板摘要、数据库/对象根目录与Worker绑定。上传purpose为library时自动添加依赖evidence的ocr/face/clip；processing仍只探测，打码/变速无需等待语义分析。

## 任务与证据边界

服务端绑定模型清单摘要、`d04-frames-v1`、设备配额与实现版本，提交和领取时复核；调用者不能用零GPU槽提交CUDA阶段或请求写入医学字段。三个模型阶段`target_fields=()`。换版本的旧任务进入待处理，不冒用新权重生成旧版本结果。

每次租约创建私有attempt目录，保留任务、请求、原生日志和结果，推理在独立Python子进程执行。取消/超时先杀子进程并等待实际退出，再由执行器释放配额。子进程在装载模型前监视父执行器进程句柄，父进程异常退出也会终止推理；Router原有租约/fencing和保守资源占用机制仍适用，不能仅凭心跳丢失认定GPU已释放。

每次最多12张PNG、单张至多2048²像素/16MiB、结果至多8MiB，外层任务限时默认300秒、日志运行中超过2MiB终止；逐帧推理控制内存，不自动扩大批量。OOM、缺权重/依赖、完整性错误明确记录，当前无盲目重试或GPU失败后降级。原生进程有时间/输入预算，尚无操作系统级硬内存限制；GPU峰值统计为torch分配量，不能当作总显存占用。

OCR输出文字、四边形和识别器分数；YuNet输出帧内框、五点位置和检测分数；CLIP输出512维归一化向量。每条记录关联真实源SHA256、帧哈希、D02归一化时间及图像尺寸，保留采样范围。稀疏帧未检出只表示该样本未检出；不证明全视频无文字/无人脸，不证明身份、方向、许可或完成脱敏。CPU/GPU耗时、进程峰值工作集与torch显存统计写入报告。

缺少D13正式医学分类快照时，CLIP仍完成编码，结果为`partial/unpublished_vocabulary`，任务进入`needs_attention`。不生成正式字段建议、不写有效值；OCR/人脸结果可独立成功。开发诊断请求可显式带`diagnostic_only=true`的中文候选，返回分字段top-k、余弦与首二间距，状态为`uncalibrated`。这不发布词表，也不进入索引。

## 字段级校准工具

`calibration.py`提供诊断用单选字段阈值、首二间距和拒识工具。候选指纹绑定完整候选ID/字段/文本、模型版本和预处理版本；换提示词或权重使旧阈值失效。多选逐标签校准、正式词表读取与规则发布须在D13/业务试点扩展，当前工具不宣称覆盖这些生产流程。

观察JSON每行包含`field, group_id, split, top_cosine, margin, correct`；group_id应是原件/来源组身份，同视频帧须同组。`correct`为显式标注判定，不从CLIP分数生成。校准集需至少两个不同来源组的阳性及两个阴性，否则不足；存在负例误接收则报告分离不足，不发布阈值。独立评测禁止使用校准来源组，不调参，按字段报告样本/接受/正确接受/错误接受/拒绝数。

在已创建且含README的私有批次目录中执行（路径为示意，须先提供真实标注输入）：

```powershell
& .\.venv\Scripts\python.exe -m media_tag.models.calibration --observations jobs/PILOT/calibration.json --fingerprint CANDIDATE_SHA256 --output jobs/PILOT/rules.json
& .\.venv\Scripts\python.exe -m media_tag.models.calibration --observations jobs/PILOT/evaluation.json --fingerprint CANDIDATE_SHA256 --rules jobs/PILOT/rules.json --output jobs/PILOT/evaluation-report.json
```

输出不可覆盖已有文件，`published=false`。余弦不是概率，无“0.8即正确”的通用阈值；未校准不选有效值，无匹配可拒识。测试使用合成分数验证分组、防泄漏、拒识和分母，不构成正式校准或医学质量指标。

## 实际工程验证与未决项

原有277项与新增32项测试涵盖真实OCR文字/空白、YuNet阳性/空白、CUDA矩阵与中文CLIP CPU/GPU对照、实际上传五阶段、纯音频不适用、版本/GPU/字段绑定、权重/帧篡改、网络/依赖/OOM故障、真实子进程取消/超时及杀父进程退出。普通wheel与61包离线安装复现另验证包内清单和本地运行库加载。

私有真实模板批次`jobs/d04-validation-20261107-3c300e81/validation.json`与最终测试结果见[进度](PROGRESS.md)。组合图上OCR/人脸/CLIP的报告耗时分别为0.922/0.125/4.672秒（含模型装载，不含外层CLI启动），进程峰值约183/88/1418MiB；torch峰值分配777347584字节（约741MiB）。这不是并发负载或P95性能指标。在公开人脸诊断图＋合成文字组合图上，OCR、人脸和CLIP实际运行，源哈希及索引revision不变，许可保持未核实。该单样本不覆盖侧脸、遮挡、医学内容、复杂版式或长视频，也不提供业务准确率承诺。

上游主要依据：[PyTorch版本安装](https://pytorch.org/get-started/previous-versions/)、[RapidOCR固定模型配置](https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml)、[YuNet模型](https://huggingface.co/opencv/opencv_zoo/tree/d4938dfc9d4ec5d098bfa33e98b3f3345a236586/models/face_detection_yunet)、[Chinese-CLIP模型](https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16/tree/36e679e65c2a2fead755ae21162091293ad37834)、[ORT安装前提](https://onnxruntime.ai/docs/install/)。模型来源/许可证记录不替代输入素材许可核实。
