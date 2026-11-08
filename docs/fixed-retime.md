# D11 固定变速与打码组合

2026-11-08：固定变速核心、受认证接口、实际音画检查及D10组合处理工程实现完成。使用项目`.venv`，没有新增依赖或数据库迁移；基础服务仍使用27包锁，视觉完整锁仍为61包。D12[人员处理页/入库](personnel-ui.md)已实现处理分支；D06/D15集成、最终常驻部署及业务画质/试听验收未完成。

## 已实现能力

- 对完整视频执行0.25～4倍固定变速，默认1.0。已实测0.25/0.5/1/1.25/2/4倍；不倒放、不生成运动插帧，保持源帧顺序及数量。
- 源身份、SHA256、源时间原点/time_base、当前输入倍速及累计倍速分别记录。独立变速不需要OCR、CLIP、人脸模型或GPU，不能在确定性阶段请求GPU槽、模型版本或医学字段写入。
- 音频可保留并同步变速、尽量保持音调，或显式去掉。多音轨必须选定一个输出轨，不能静默取第一轨。未选中的流不进入输出，选轨及实际输出流编号在报告中记录。
- 原件、尺寸、文件哈希及已确认索引值不改；输出通过独立探测、全片解码及D09受信采用后登记为新的私有衍生版本。D11不确认来源许可、医学标签或当前剪辑用途。
- 同源/同配方/版本及有效产物按哈希复用；重复请求不再对上次输出叠加变速。修改速度应以原登记基准提交。选择衍生输入必须显式`chain_from_derived=true`，此时记录累计倍速，不隐式取回原件或绕过其权限。

源码：`transforms/retime.py`为共享编码与校验函数，`retime_service.py`负责计划/队列/受信采用；D10与D11复用`transforms/runtime.py`中的Windows受控进程树。运行状态仍在D03 Router SQLite，批次JSON只作证据，不新增手工状态队列。

## 时间、片段和音轨

源时间以D09登记的所选视音频共同原点归一；输出位置为`源归一化位置 / speed`，总时长为源共同跨度除以倍速。视频原始PTS、每帧持续时间、音轨起始偏移及持续时间分别变换。VFR逐帧缩放时间戳，最后一帧持续时间显式设置，再重新探测全部输出帧核对数量、PTS和持续时间；不把容器时长当唯一证据。

`preserve_pitch`使用FFmpeg `atempo`；超过单个0.5～2范围时串联，例如4倍为2×2、0.25倍为0.5×0.5。音频在变速后按登记的目标时长补尾部静音或裁剪编码边界，再加回缩放后的共同原点偏移。此策略写入报告，不声称有损AAC编码是采样级无损复制。音轨实际起止允许`0.01 + 2048 / sample_rate`秒的编码粒度容差；合成案例还另行测量解码PCM信号的时刻与音调。

内置`SourceSegment`为内部片段注记：segment_id、start_seconds、end_seconds，均基于当前输入的共同源时钟。报告返回source/output两组位置。它不裁剪文件、不改正式表格字段或人工医学有效值。独立变速`segments=null`/省略时继承父变体已记录的注记，并先映射到父文件实际输出时间再缩放；显式`[]`表示不携带内部注记。重复ID、反向区间和超出源范围会被拒绝。

## 与人脸打码组合

D10的`POST /redaction/renders`新增`speed`（默认1.0）和`segments`，仍使用固定源扫描/修订哈希。遮罩先应用到每个源帧，再按同一时间映射编码；遮罩和风险时间同时记录源/输出位置。视频编码一次，之后音轨封装复制视频流，不再做第二次有损视频编码。

本次组合渲染版本为`d11-redaction-retime-v1`。原有已登记输出及其复核不改；旧排队渲染版本由Router拒绝冒用新实现，需要重新规划。有效的原始D10扫描可继续复用。修改速度/遮罩产生新配方、新输出，旧哈希上的批准不能用于新文件。

对已经打码的衍生文件做独立变速，也会在新输出上重新要求隐私复核。服务端在计划及采用时检查完整隐私来源链；通过通用计划接口填写`privacy_review_required=false`也不能清除此门槛。普通下载、预览及下游版本继续经过D17逐对象/来源链检查，失败不回退原件。未复核/撤回输出仅在专用私有复核入口按权限查看；真实复核人仍需明确绑定，默认没有批准者。

## 启用及接口

已有D03本地配置增加以下字段即可独立运行，不要求存在模型目录：

```json
"models": {"enabled": false},
"redaction": {"enabled": false},
"retime": {"enabled": true, "reviewers": []}
```

组合打码时另按[D10说明](face-redaction.md)启用redaction并提供绝对模型目录。retime.reviewers只用于变速后的隐私输出复核，须配置真实editor/admin身份及该对象read授权；普通变速不因此获得人脸处理状态或团队共享权限。

```powershell
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
& .\.venv\Scripts\python.exe -m media_tag serve --config config/YOUR.local.json
# 另一个终端执行一次已排队任务，不代表已安装常驻Worker。
& .\.venv\Scripts\python.exe -m media_tag work-once --config config/YOUR.local.json --worker-id YOUR_WORKER_ID
```

| 方法/路径 | 内容 |
| --- | --- |
| `POST /retime/jobs` | request_id、parent_asset_id、expected_revision；可选speed、audio、audio_stream_index、chain_from_derived、segments、options。返回固定计划及job，不接受任意FFmpeg参数、角色或模型配置 |
| `GET /retime/jobs/{job_id}` | 真实任务/采用状态、asset_id、源哈希、time_map、累计速度、更新后的片段、实际帧/音频/资源报告；`apply_transform_again=false`，下载方不得再应用一次变速 |
| `GET/HEAD /retime/outputs/{asset_id}/preview-content?sha256=…` | 隐私受限变速输出的私有复核预览，支持Range；一般无隐私门槛的变体使用D17常规预览/下载入口 |
| `POST /retime/outputs/{asset_id}/review` | 复用D10的输出SHA256/recipe_hash/CAS/指定复核人协议，批准、撤回均可审计，不继承父文件批准 |
| `POST /redaction/renders` | 在D10原参数上增加speed/segments，完成单独打码或打码＋变速；输出仍需实际哈希复核 |

所有retime响应禁止共享缓存。`report`为经哈希校验的实际执行报告；尚无等价已登记产物且采用未完成时asset_id为空，不把执行成功当文件已经可用；返回另一等价执行的既有产物时标记output_reused，仍检查来源及生产者门槛。`file_access`表示当前文件政策门槛，实际下载仍校验后端与字节；不会伪装内容可达。

## 预算与边界

默认源共同跨度60秒、1800帧、单帧1920²像素及累计10亿像素，均同时生效；严格上限可按请求提高至600秒/7200帧/4096²/80亿像素，仍受Router及外部工具限时约束。超限、损坏、时钟矛盾、非方形像素比、未指定多音轨、过期租约等明确失败，不截取一段后宣称完整处理。Windows Job Object先绑定进程树再放行，取消/超时确认包括FFmpeg子孙进程已停止后释放CPU槽。

输出为H.264/yuv420p/MP4、音频AAC；当前以SDR合成片验证。慢放会降低实际播放帧率，未用插帧补出不存在的动作。HDR、隔行、复杂声道及真实语音/音乐主观质量尚未验收。D12人员处理页已实现处理分支，最终主机/常驻服务/容量与恢复留D07。

## 实际验证

D11新增40项，项目环境及普通wheel环境全量382项通过。覆盖六种速度、实际解码PCM音调、音频标记/视频PTS、前后起始偏移、VFR/旋转/非零原点、多音轨选择/去音频、默认速度/重复/显式链式、片段继承/遮罩时间、单次编码及新哈希隐私复核。复用了D10的取消/超时/进程树与D09事务/产物校验回归。

另在新建的27包基础环境运行36项独立变速测试，并完成排除视觉专用用例的315项核心回归，确认torch/ORT/RapidOCR/OpenCV/psutil均未安装，实际服务与CLI仍可执行；没有为D11下载模型或增加GPU依赖。

真实六模板批次：`jobs/d11-validation-20261108-27debc0d/validation.json`（忽略Git）。纯CPU环境实际执行0.5/1.25/2/默认1倍，测量解码后的两次全帧闪烁与440Hz脉冲能量中心，8组配对的最大音画偏差约18.98毫秒；完整下载哈希/Range、同配方复用及默认速度不继承2倍通过。另在项目`.venv`实际运行12帧YuNet扫描及1.25倍马赛克组合，视频编码一次，输出保留pending，普通入口拒绝、私有预览哈希通过。源SHA256及索引revision不变，隐私批准事件为0。

合成标记验证不等于真实业务素材的A05试听/画质签收，也不替代A04全片人脸输出复核；这些需提供授权素材和指定人员，经D12/D07完成。

主要实现依据：[FFmpeg滤镜](https://www.ffmpeg.org/ffmpeg-filters.html)、[帧时间选项](https://www.ffmpeg.org/ffmpeg-all.html)、[setts位流滤镜](https://ffmpeg.org/ffmpeg-bitstream-filters.html)。
