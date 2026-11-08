# media-tag

供剪辑人员和 AI 剪辑共同使用的素材库：素材登记与后台打标、口语/专业词检索、独立人脸打码和视频变速、可追溯版本下载及表格导出。后续接入飞书并支持文件迁移到 OSS。

截至 **2026-11-08**，D00～D04、D06、D09～D15已完成工程验收，新增持久化任务队列、租约/fencing、资源配额、受认证HTTP控制接口及本地CPU执行适配。已有SQLite事务/权限/修订、本地不可变副本，以及真实工具探测、透明/音轨检查和带实际时间戳的稀疏帧。六份原始模板已绑定摘要，完整医学分类词表仍未发布。D09已实现断点上传、完成校验/自动分析入队、衍生配方/时间映射与授权版本列表。D17授权文件/预览/下载与公共分页检索API已实现（[说明](docs/asset-search-api.md)），真实宿主/跨端业务验收仍待后续。D04已实现OCR/人脸/CLIP真实推理与队列适配；D10已实现完整视频扫描/轨迹修订、两种遮罩、私有预览与绑定哈希的人工复核接口；D11已实现固定变速、保音调/去音轨、时间映射与打码组合；D12人员页面、轨迹编辑、主动入库与指定输出复核已实现，D06字段复核/证据/CSV已接入，D15搜索/词表管理已接入，业务验收仍待提供；常驻部署、真实MCP宿主及整体联调仍未完成。D00～D20共21张任务卡，首期19张、后续/可选2张，工程任务已完成14张；任务数量不表示整体工时完成比例。

## 项目总控

| 维度 | 文档 | 用途 |
| --- | --- | --- |
| 需求 | [需求基线](docs/REQUIREMENTS.md) | R01～R15、N01～N06、已确认范围、Q01～Q07待定项 |
| 设计 | [设计总览](docs/ARCHITECTURE.md) | 模块边界、业务链路、身份/版本、推荐部署与AI接入 |
| 实施 | [实施计划](docs/IMPLEMENTATION_PLAN.md) | M0～M6、D00～D20依赖、Router/Worker下发与阶段出口 |
| 进度 | [进度与待办](docs/PROGRESS.md) | 实际产物、逐任务状态、近期队列、证据与剩余问题 |

用户已选定极简画廊为主、列表可切换，打码/变速独立操作，标签后台执行；术语采用词表＋文本小模型。Windows常驻服务、具体工程栈及API＋薄MCP是推荐设计，尚未作为已部署事实。

本机使用根目录 `.venv`（Python 3.12.14），核心开发锁含27包，D04完整视觉锁含61包，版本及wheel哈希已锁定；本机实际CUDA/中文图文推理通过，见[D04说明](docs/model-workers.md)。FFmpeg/ffprobe 9.0.2位于项目忽略缓存，无需系统PATH。环境见 [开发环境说明](docs/DEVELOPMENT_ENVIRONMENT.md)，模板基线见 [D00契约说明](docs/contracts.md)，实际媒体工具与分析接口见 [D02说明](docs/media-evidence.md)，队列/API及启动配置见 [D03说明](docs/router-queue.md)，上传/衍生契约见 [D09说明](docs/upload-variants.md)，固定版本详情/预览/下载与Range见 [D17文件基础](docs/file-access.md)。[D13词表契约与发布](docs/terminology-release.md)已实现候选/逐条审核、快照/差异及配对回滚；[D14文本模型与术语匹配](docs/terminology-worker.md)已实现固定BGE CPU推理、配套索引/降级与Worker；[D15检索与反馈管理](docs/search-feedback.md)已接入人员页，[D16评测工具](docs/terminology-evaluation.md)已实现分组防泄漏、对照指标和离线校准；正式候选及独立业务集仍待补充。

[D01索引与存储说明](docs/catalog-storage.md)列出核心库与权限边界。当前未建立生产数据库、未批量导入真实素材；最近一次全量621项Python工程测试、最终13项维护测试及既有4项JavaScript测试涵盖临时合成数据、公开诊断图及真实本机模型推理；正式业务样本与独立集仍待提供。D02/D03/D04/D06/D09/D10/D11/D12/D17文件基础另有私有jobs工程验证批次，不构成真实内容或医学验收。

```powershell
& .\.venv\Scripts\python.exe -m pytest -q # 需六份本地原始模板、视觉依赖、权重及固定诊断图；仅核心环境用 --ignore=tests/models --ignore=tests/redaction --ignore=tests/retime/test_combined.py --ignore=tests/web/test_privacy.py --ignore=tests/export/test_privacy.py --ignore=tests/terminology/embedding/test_inference.py
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
& .\.venv\Scripts\python.exe -m media_tag contracts schema --model task
```

## 专题与契约

- [存储、字段与验收专题](docs/DESIGN_AND_IMPLEMENTATION.md)：Windows/OSS、A01～A11及AI取材D17～D20/I01～I08。
- [模型与任务执行专题](docs/MODEL_ROUTER_WORKER_PLAN.md)：模型候选、运行协议与D00～D08。
- [上传与视频处理专题](docs/UPLOAD_AND_MEDIA_VARIANTS.md)：人脸、变速、版本及D09～D12。
- [术语与检索专题](docs/TERMINOLOGY_MAPPING_PLAN.md)：词表维护、D13～D16及T01～T12。
- [项目工作约定](AGENTS.md)、[本地原始表头与规则](asset-index-template/README.md)、[文档目录与维护规则](docs/README.md)。

范围与顺序以总控文档为准，进度只看实际证据。D00本地命令、D01核心库、D02技术分析、D03调度控制API及D09上传/衍生契约已实现；D04模型、[D10打码核心与复核](docs/face-redaction.md)、D17文件基础已实现；[D11固定变速/组合](docs/fixed-retime.md)已实现；[D12人员页与入库](docs/personnel-ui.md)已实现处理分支，配置`web_enabled=true`后访问`/ui/`；[D06字段复核与CSV](docs/review-csv.md)已实现，[D15搜索与反馈管理](docs/search-feedback.md)已实现；D16独立效果评测、D17公共语义契约已实现，D18只读MCP适配已实现，真实宿主与常驻部署仍待后续。

参考相邻剪辑项目的工作组织方式，本机实际仓库为 `D:/ai/auto-video`；不继承其处理授权，不修改相邻项目。D19已实现本仓库任务内固定字节下载/来源记录/当前用途预览交接，实际宿主集成仍待明确。

Git保存代码、结构定义、合成测试夹具和文档。原始模板、运营词表、素材、数据库、运行记录、导出结果与凭证保存在忽略目录或仓库外。

asset-index-template/ 由用户本地提供，整个目录忽略Git；早期提交仍含历史副本。新克隆需另行放入经授权的模板，缺失时明确报告，不能用示例编造正式契约。

[D18只读MCP桥接](docs/mcp-adapter.md)提供四个受权取材工具，固定官方SDK2.3.0，Windows44包MCP开发锁已离线复现；标准现代/legacy客户端和真实stdio通过，实际AI宿主尚未配置。

[D19剪辑来源适配](docs/editing-provider.md)已实现新任务边界、断点恢复、完整哈希/解码、缓存重新授权和绑定稿件/片段/证据的preview handoff；真实81次HTTP/9次CPU任务及CLI下载通过。交接保持预览终点和医学待审，实际剪辑宿主与完整预览仍待联调。

[D20本仓库完整预览演练](docs/integration-rehearsal.md)通过真实stdio/API/固定下载及1.5秒24帧合成预览；实际宿主/跨机器和业务验收仍待补充。

[D07离线维护](docs/offline-recovery.md)提供备份/校验/隔离还原CLI，真实本机恢复后HTTP/BGE、素材值/权限/版本/配对已验证；最终主机、常驻服务与业务综合签收仍待条件。
