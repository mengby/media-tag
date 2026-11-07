# media-tag

供剪辑人员和 AI 剪辑共同使用的素材库：素材登记与后台打标、口语/专业词检索、独立人脸打码和视频变速、可追溯版本下载及表格导出。后续接入飞书并支持文件迁移到 OSS。

截至 **2026-11-08**，D00～D04及D09～D11已完成工程验收，新增持久化任务队列、租约/fencing、资源配额、受认证HTTP控制接口及本地CPU执行适配。已有SQLite事务/权限/修订、本地不可变副本，以及真实工具探测、透明/音轨检查和带实际时间戳的稀疏帧。六份原始模板已绑定摘要，完整医学分类词表仍未发布。D09已实现断点上传、完成校验/自动分析入队、衍生配方/时间映射与授权版本列表。D17授权文件、预览与下载基础接口已实现，整项仍在实施中。D04已实现OCR/人脸/CLIP真实推理与队列适配；D10已实现完整视频扫描/轨迹修订、两种遮罩、私有预览与绑定哈希的人工复核接口；D11已实现固定变速、保音调/去音轨、时间映射与打码组合；前端、常驻部署、语义检索/MCP及业务验收仍未完成。D00～D20共21张任务卡，首期19张、后续/可选2张，工程任务已完成8张；任务数量不表示整体工时完成比例。

## 项目总控

| 维度 | 文档 | 用途 |
| --- | --- | --- |
| 需求 | [需求基线](docs/REQUIREMENTS.md) | R01～R15、N01～N06、已确认范围、Q01～Q07待定项 |
| 设计 | [设计总览](docs/ARCHITECTURE.md) | 模块边界、业务链路、身份/版本、推荐部署与AI接入 |
| 实施 | [实施计划](docs/IMPLEMENTATION_PLAN.md) | M0～M6、D00～D20依赖、Router/Worker下发与阶段出口 |
| 进度 | [进度与待办](docs/PROGRESS.md) | 实际产物、逐任务状态、近期队列、证据与剩余问题 |

用户已选定极简画廊为主、列表可切换，打码/变速独立操作，标签后台执行；术语采用词表＋文本小模型。Windows常驻服务、具体工程栈及API＋薄MCP是推荐设计，尚未作为已部署事实。

本机使用根目录 `.venv`（Python 3.12.14），核心开发锁含27包，D04完整视觉锁含61包，版本及wheel哈希已锁定；本机实际CUDA/中文图文推理通过，见[D04说明](docs/model-workers.md)。FFmpeg/ffprobe 9.0.2位于项目忽略缓存，无需系统PATH。环境见 [开发环境说明](docs/DEVELOPMENT_ENVIRONMENT.md)，模板基线见 [D00契约说明](docs/contracts.md)，实际媒体工具与分析接口见 [D02说明](docs/media-evidence.md)，队列/API及启动配置见 [D03说明](docs/router-queue.md)，上传/衍生契约见 [D09说明](docs/upload-variants.md)，固定版本详情/预览/下载与Range见 [D17文件基础](docs/file-access.md)。前轮术语候选数据仍未提供。

[D01索引与存储说明](docs/catalog-storage.md)列出核心库与权限边界。当前未建立生产数据库、未批量导入真实素材；382项工程测试涵盖临时合成数据、公开诊断图及真实本机模型推理；正式业务样本与独立集仍待提供。D02/D03/D04/D09/D10/D11/D17文件基础另有私有jobs工程验证批次，不构成真实内容或医学验收。

```powershell
& .\.venv\Scripts\python.exe -m pytest -q # 需视觉依赖、权重及固定诊断图；仅核心环境用 --ignore=tests/models --ignore=tests/redaction --ignore=tests/retime/test_combined.py
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
& .\.venv\Scripts\python.exe -m media_tag contracts schema --model task
```

## 专题与契约

- [存储、字段与验收专题](docs/DESIGN_AND_IMPLEMENTATION.md)：Windows/OSS、A01～A11及AI取材D17～D20/I01～I08。
- [模型与任务执行专题](docs/MODEL_ROUTER_WORKER_PLAN.md)：模型候选、运行协议与D00～D08。
- [上传与视频处理专题](docs/UPLOAD_AND_MEDIA_VARIANTS.md)：人脸、变速、版本及D09～D12。
- [术语与检索专题](docs/TERMINOLOGY_MAPPING_PLAN.md)：词表维护、D13～D16及T01～T12。
- [项目工作约定](AGENTS.md)、[本地原始表头与规则](asset-index-template/README.md)、[文档目录与维护规则](docs/README.md)。

范围与顺序以总控文档为准，进度只看实际证据。D00本地命令、D01核心库、D02技术分析、D03调度控制API及D09上传/衍生契约已实现；D04模型、[D10打码核心与复核](docs/face-redaction.md)、D17文件基础已实现；[D11固定变速/组合](docs/fixed-retime.md)已实现；专题中的语义检索、常驻部署及其他任务交付仍为拟定。

参考相邻 doctor-video-automation 的工作组织方式，不继承其处理授权，不修改相邻项目。AI取材的跨仓库实现范围在实际下发D19时明确。

Git保存代码、结构定义、合成测试夹具和文档。原始模板、运营词表、素材、数据库、运行记录、导出结果与凭证保存在忽略目录或仓库外。

asset-index-template/ 由用户本地提供，整个目录忽略Git；早期提交仍含历史副本。新克隆需另行放入经授权的模板，缺失时明确报告，不能用示例编造正式契约。
