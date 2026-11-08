# D19 本机剪辑来源接入

2026-11-08：用户确定`E:/asset`保存取材，剪辑项目仍为`D:/ai/auto-video`。已在两仓库实施CLI来源接入；AI客户端沿用Codex/D18，库服务仍为本机8765。没有移动素材库数据库/对象仓或修改Codex全局入口。

## 已安装入口与目录

剪辑仓库`scripts/media_tag_source.py`提供`init-job/search/fetch`；通过私有`config/media-tag.local.json`调用本项目`.venv/Scripts/python.exe -m media_tag editing-source`。配置示例在剪辑仓库`config/media-tag.example.json`，只包含解释器、reader客户端配置、FFmpeg及两个项目的绝对路径；令牌值不复制到剪辑仓库或E盘。当前私有配置已恢复指向正式空库，工程夹具未导入正式服务。

`E:/asset`仅授权EDY和SYSTEM，是外置取材目录。`jobs/editing-<绑定摘要>/edit/media-tag/`保存固定哈希原件、part、receipt和锁，按剪辑任务与Acquisition绑定分隔。实际技术/解码/Range/哈希校验复用既有D19 TaskProvider；重复相同请求重新验权、验证缓存并复用，不重复变速。

剪辑任务内`edit/media/`保存单独复制的原件及现有眼部隐私处理副本；不跨盘硬链接，不改写E盘原件。登记明确provider=media-tag，保留源asset/root/parent、文件与处理哈希、源时钟/累计速度、来源、检索版本、用途以及隐私处理链。库的打码授权不冒充剪辑端眼部处理或当前用途通过。

## 新任务操作

在实际EDY的PowerShell中，从剪辑项目运行：

```powershell
& D:\ai\media-tag\.venv\Scripts\python.exe scripts/media_tag_source.py init-job NEW_JOB
& D:\ai\media-tag\.venv\Scripts\python.exe scripts/media_tag_source.py search NEW_JOB --input D:\ABS\query.json
& D:\ai\media-tag\.venv\Scripts\python.exe scripts/media_tag_source.py fetch NEW_JOB --input D:\ABS\fetch.json
```

命令已可运行；NEW_JOB和输入路径是待填写占位。`init-job`必须先于普通任务流程创建该新目录；已存在任务明确拒绝，不迁移旧任务，不为现有UI任务自动补标记。初始化后由原剪辑流程准备稿件、粗剪与包装。该接入是显式CLI来源，不改变现有UI任务创建器或自动安排剪辑任务。

检索输入采用D17 AssetSearch，例如`{"query":"红色圆形","literal_only":true,"use_model":false}`。真实查询、返回版本和结果写当前任务`edit/media-tag/search-<摘要>.json`；服务失败明确报错，不记成无匹配。原项目的百度优先、公共来源回退和整片检索审计仍保留；media-tag属于本地库取材入口，不能据此宣称已完成其他来源检索。

fetch输入为`{"id":"素材局部ID","acquisition":...}`，Acquisition结构见[原D19契约](editing-provider.md)。context须绑定当前`edit/cut.transcript.json`与`edit/automation/runs/<RUN>/candidate.json`的实际SHA256；保留unit_id、医生原话、用途、成片窗口，视频另给所下载版本的source_interval。不同用途换局部ID，同一ID不可覆盖其他请求。只接收PNG/JPEG/WebP/MP4，GIF先显式转换，不暗中改时钟或循环补时长。

fetch完成后返回待内部检查的media item，登记仍是candidate；由原包装流程合入本运行目录candidate，不能作为页面已采用结果。现有`scripts/register_candidate.py`真实执行人脸处理，不因新来源绕过；隐私处理失败留缺口，不回退未处理原件。当前新增视频依赖遵循剪辑项目固定PyAV16.0.1，已加入本项目`.venv`；增量Windows锁见[依赖文件](../config/requirements-editing-win-py312.txt)，官方文件与摘要来自[PyPI](https://pypi.org/project/av/16.0.1/)。其他核心/视觉锁不变。

## 当前用途与预览边界

真正看图/看片并按原项目十项要求核对后，使用原`accept_materials.py`，检查单另须`actual_media_observed:true`，准确绑定当前candidate/转录、实际入片文件、原话/用途/unit和片段。不生成自动pass，不把download或标签确认当作验收；调用方观看声明仍是声明，脚本不能证明AI确实看过。

验收和`media_validation.py`均经桥接重新查固定版本权限、revision、技术/配方/来源；核验host文件及隐私链、转录和时间。服务离线、撤权、源元数据改变、稿件改变、处理输出改变都拒绝，已下载字节不是永久权限。正式服务空库时只返回正常空结果。

原`preview_only:true`、医学pending、暂停正式导出和现有许可开关均保持原值；不删除或改写旧审核/视频，不回写共享assets。许可恢复严格模式后仍按原必填凭据校验，桥接不编造许可。D19不执行check_stage自动应用、Remotion完整预览或最终导出，D20真实剪辑任务另验。

## 实际验证

最终工程批次`jobs/d19-editing-7619cd38/validation.json`：实际E盘图片及2倍速副本下载、真实剪辑端眼部处理、来源登记、原accept_materials和media_validation通过；重复请求复用、未观看声明拒绝、复制后撤权拒绝，原件/模板不变。82次真实HTTP、9次CPU任务、7次实际剪辑CLI；4个原策略/校验入口文件摘要不变。测试检查单明确为合成契约数据，真实AI观看、真实医学素材/当前用途及完整成片预览没有冒充通过。

本项目全量643项、最终12项新增边界测试、既有相关78项，剪辑端4项新增与23项原媒体测试通过；原预览/正式导出策略17项Node测试通过。运行时须沿用原`EDITOR_PYTHON`配置，本机验证显式设为项目`.venv`；Windows Python检查用PYTHONUTF8=1，未修改全局环境。纯核心普通wheel环境39项通过，91份Markdown与131模块/资源一致；全量回归与打包结果见[总进度](PROGRESS.md)。

剪辑仓库本轮本地提交为`06a9857cc613f31eb958f0e66301a032bcde7305`。
