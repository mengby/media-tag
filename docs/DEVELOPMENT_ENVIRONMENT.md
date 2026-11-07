# 本机开发环境与开工步骤

核实日期：2026-11-07（Asia/Shanghai）。已建立本机Python虚拟环境并完成D00工程验收，真实模板校验和本地命令见 [契约说明](contracts.md)；业务服务、模型与部署尚未验收。阶段与任务顺序继续以 [实施计划](IMPLEMENTATION_PLAN.md) 为准。

## 已建立的环境

| 项目 | 本机实测 |
| --- | --- |
| 项目目录 | `D:\ai\media-tag` |
| 虚拟环境 | 根目录 `.venv`，Python 3.12.14；不继承系统 site-packages |
| 项目解释器 | `D:\ai\media-tag\.venv\Scripts\python.exe` |
| 基础解释器 | `C:\Users\EDY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` |
| pip | 25.0.1；`pip check`通过；D00依赖已安装，14个包按版本及wheel哈希锁定，未安装业务服务/模型依赖 |
| SQLite | Python 标准库可用，SQLite 3.53.1；未创建业务数据库 |
| 本机 GPU | `nvidia-smi` 返回 RTX 5060 Ti、8151 MiB、驱动 580.97；不是模型兼容性验收 |
| 外部命令 | 当前 PATH 未找到 python、py、uv、ffmpeg、ffprobe、node、npm；基础 Python 通过绝对路径调用 |

本机显卡读数与需求中用户提供的 RTX 5060 记录不同；本机检查不能证明最终常驻服务器就是这台机器，也不据此改写目标硬件要求。尚未执行 CUDA 运算、模型推理或编码检查。

`.venv` 依赖上述基础解释器；Codex 缓存清理、解释器迁移后应重建。它是本机开发环境，不作为正式部署运行时。目标机器需使用独立维护的 Python 安装，在目标项目目录重新创建 `.venv`，不能复制当前虚拟环境过去。Python 3.12.14 是本次可用环境记录，业务及 GPU 依赖支持范围仍须在 D00/D07 实测并锁定。

## 使用方式（已验证）

在 PowerShell 中进入项目后，可以始终显式调用项目解释器，无需激活：

```powershell
Set-Location D:\ai\media-tag
& .\.venv\Scripts\python.exe --version
& .\.venv\Scripts\python.exe -m pip --version
& .\.venv\Scripts\python.exe -m pip check
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

以下为实施落点；D00已完成工程验收，其他业务仍待开发。选型用于当前工程实施，不代表目标部署已完成。

1. **M0：D00＋D07 环境准备。** D00的原始契约、Python工程、依赖锁与合成测试已验收。后续核实最终服务器、数据/备份目录与网络，配置FFmpeg/ffprobe并实测；不将D00完成等同于M0全部完成。
2. **M1：D01/D02/D03/D09，D17 基础接口。** 先做一条真实素材的上传→完整性校验→SHA256 去重→来源登记→技术探测→授权预览/固定版本下载。最早的可演示交付是这条闭环，带失败记录与恢复；不是先堆模型或做模拟页面。
3. **M2：D04 人脸子交付、D10/D11/D12。** 先打通独立视频处理页：单独变速、单独打码及组合。保留原件，处理结果另存并登记配方、时间映射和哈希；输出复核绑定实际文件。
4. **M3：D04/D06/D13/D14/D15，完成 D12。** 接入真实 OCR/CLIP 与后台任务、人工有效值保护、契约导出、词表审核发布、小模型召回及画廊/列表检索。医学标签确认与打码输出复核分别记录。
5. **M4/M5：完成 D17，D18～D20、D16/D07。** 明确真实 AI 宿主及跨仓库范围后接 API/MCP 和剪辑下载适配，完成独立评测、部署、权限、故障恢复与备份还原。飞书/OSS 真接入及可选 VLM 留在 M6。

核心 API/调度/存储依赖进入 `.venv`。视觉/文本推理依赖先验证冲突与 GPU 兼容性；需要隔离时再建立单独 Worker 环境并登记锁文件，不直接把全部模型包装进核心环境。FFmpeg 是外部程序，前端工具链也不由 Python 虚拟环境提供。

本轮D00已补真实契约解析、说明表顺序/归属与字节基线校验；工程验收完成。后续推进D01及D07准备，完整分类/术语词表按D13发布；真实媒体与业务验收仍按各任务要求执行。
