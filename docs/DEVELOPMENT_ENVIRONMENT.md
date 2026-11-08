# 本机开发环境与开工步骤

核实日期：2026-11-07（Asia/Shanghai）。项目`.venv`用于D00～D04、D09～D11及D17文件基础工程，见 [契约说明](contracts.md)、[索引与存储](catalog-storage.md)、[技术提取与证据](media-evidence.md)、[Router与队列](router-queue.md)、[上传与衍生契约](upload-variants.md)、[D17文件基础](file-access.md)；HTTP上传/调度、衍生契约、授权文件/预览/Range已通过工程验证，D04真实模型与本机CUDA已验证，详见[模型说明](model-workers.md)；正式业务及部署尚未验收。D01使用标准库sqlite3，D02新增Pillow及独立便携FFmpeg工具，D03新增FastAPI/Uvicorn与HTTPX测试依赖。阶段与任务顺序继续以 [实施计划](IMPLEMENTATION_PLAN.md) 为准。

## 已建立的环境

| 项目 | 本机实测 |
| --- | --- |
| 项目目录 | `D:\ai\media-tag` |
| 虚拟环境 | 根目录 `.venv`，Python 3.12.14；不继承系统 site-packages |
| 项目解释器 | `D:\ai\media-tag\.venv\Scripts\python.exe` |
| 基础解释器 | `C:\Users\EDY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` |
| pip | 25.0.1；`pip check`通过；核心27包锁不变；D04完整视觉锁61包已装入项目`.venv`并离线复现，含torch 2.9.1+cu128、Transformers 4.57.6、RapidOCR 3.9.2、ORT 1.23.2 |
| SQLite | Python 标准库可用，SQLite 3.53.1；未创建业务数据库 |
| 本机 GPU | `nvidia-smi` 返回 RTX 5060 Ti、8151 MiB、驱动 580.97；已实际运行sm_120 CUDA矩阵及中文CLIP；不是最终部署验收 |
| 外部命令 | 初始PATH无python/py/uv/ffmpeg/ffprobe/node/npm；Python用项目`.venv`，FFmpeg/ffprobe 9.0.2用项目cache中的固定路径，不依赖系统PATH |

本机显卡读数与需求中用户提供的 RTX 5060 记录不同；本机检查不能证明最终常驻服务器就是这台机器，也不据此改写目标硬件要求。已执行CUDA矩阵、OCR/YuNet及Chinese-CLIP CPU/CUDA图文编码；未执行最终服务器部署验收。系统旧MSVC不兼容ORT，通过`.venv`内msvc-runtime解决，未改系统库或驱动。

`.venv` 依赖上述基础解释器；Codex 缓存清理、解释器迁移后应重建。它是本机开发环境，不作为正式部署运行时。目标机器需使用独立维护的 Python 安装，在目标项目目录重新创建 `.venv`，不能复制当前虚拟环境过去。Python 3.12.14 是本次可用环境记录，业务及 GPU 依赖支持范围仍须在 D00/D07 实测并锁定。

## 使用方式（已验证）

在 PowerShell 中进入项目后，可以始终显式调用项目解释器，无需激活：

```powershell
Set-Location D:\ai\media-tag
& .\.venv\Scripts\python.exe --version
& .\.venv\Scripts\python.exe -m pip --version
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
```

也可以按需运行 `& .\.venv\Scripts\Activate.ps1`，再调用 `python`；此激活方式尚未实测。遇执行策略限制时使用上面的解释器绝对/相对路径方式即可。编辑器选择 `.venv\Scripts\python.exe`。

本次创建命令为：

```powershell
& 'C:\Users\EDY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv .venv
```

仅在环境不存在时创建；不对已有环境覆盖执行。其他机器应改用该机器已安装的基础 Python。`.gitignore` 已忽略 `.venv/`，不提交解释器和安装包。

## 本地契约与后续输入

用户已补齐本地 `asset-index-template/` 的六个原始文件，D00已实际读取并校验；没有从历史提交恢复文件：

- `README.md`
- `素材库打标方案.md`
- `feishu_asset_index_fields.csv`
- `feishu_asset_index_template.csv`
- `feishu_gap_table_fields.csv`
- `feishu_gap_table_template.csv`

这些文件保持原样且继续忽略Git。真实模板35/14列及说明表同序已校验，内容指纹见契约说明。科室、器官/系统、疾病/症状仍缺完整分类选项，D13须审核发布，不能从说明中的省略项/示例编造正式词表。

前轮记录的 `data/terminology/`、`outputs/01a1b49f-9d78-77a3-b29c-32fb255fd5b7/` 本机也不存在。候选表可在 D13 前另行提供，不阻止先做工程基础；不从文档中的条数重建正式数据。

## 拟定实施落点

以下为实施落点；D00～D04及D09已完成各自工程验收，其他任务按依赖推进。选型用于当前工程实施，不代表目标部署已完成。

1. **M0：D00＋D07 环境准备。** D00契约/工程已验收，本机FFmpeg/ffprobe与CPU合成解码已在D02核实。本机模型/GPU已实测；最终服务器、数据/备份目录、网络与恢复仍待实测；不将本机检查等同于M0全部完成。
2. **M1：D01/D02/D03/D09，D17 基础接口。** 先做一条真实素材的上传→完整性校验→SHA256 去重→来源登记→技术探测→授权预览/固定版本下载。最早的可演示交付是这条闭环，带失败记录与恢复；不是先堆模型或做模拟页面。
3. **M2：D04 人脸子交付、D10/D11/D12。** 先打通独立视频处理页：单独变速、单独打码及组合。保留原件，处理结果另存并登记配方、时间映射和哈希；输出复核绑定实际文件。
4. **M3：D04/D06/D13/D14/D15，完成 D12。** 接入真实 OCR/CLIP 与后台任务、人工有效值保护、契约导出、词表审核发布、小模型召回及画廊/列表检索。医学标签确认与打码输出复核分别记录。
5. **M4/M5：完成 D17，D18～D20、D16/D07。** 明确真实 AI 宿主及跨仓库范围后接 API/MCP 和剪辑下载适配，完成独立评测、部署、权限、故障恢复与备份还原。飞书/OSS 真接入及可选 VLM 留在 M6。

核心 API/调度/存储依赖进入 `.venv`。D04视觉依赖已验证冲突及GPU兼容性，按用户要求使用项目`.venv`，完整视觉锁与核心锁分开维护；文本模型依赖尚未安装。模型按租约隔离为子进程，不在API启动时加载。FFmpeg 是外部程序，D12前端为随wheel分发的原生资源，无Node生产依赖；浏览器测试另用本机Node与运行时捆绑的Playwright控制本机Chromium，不加入Python锁。

D00～D04及D09工程已验收，D17文件基础已完成工程验证，D10打码核心/复核与Windows进程树已实测；D11于2026-11-08补验固定变速/组合及27包CPU环境；D12处理页/入库分支已验证，D06字段复核/CSV已接入，后续D13/D14/D15及D17语义前置，D07准备继续。本机FFmpeg CPU合成解码已验证，最终服务器/GPU/容量与恢复未验；完整分类/术语词表按D13发布，真实素材与业务验收仍按各任务要求执行。

2026-11-08补充：D13词表契约/审核/发布回滚工程已验收，无新增依赖；项目`.venv`500项、纯CPU431项及普通wheel集成157项通过。真实HTTP批次与边界见 [D13说明](terminology-release.md)。正式分类与候选来源/真实管理人仍待提供，D14文本模型和D15页面未实现。

2026-11-08 D14：沿用现有`.venv`，无新包版本；新增46包文字依赖子集锁和BGE约91.4MiB权重，真实CPU中文512维/进程复用/超时清理通过。完整配置及工程验证见 [D14说明](terminology-worker.md)。
