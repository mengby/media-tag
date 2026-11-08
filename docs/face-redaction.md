# D10 完整视频人脸打码

2026-11-07：模型/处理核心、受认证接口、私有预览与输出复核工程实现完成。使用项目`.venv`及D04的61包视觉锁，无新增第三方依赖。源码为`workers/face_track.py`与`transforms/`；D12[轨迹编辑与入库页面](personnel-ui.md)已实现处理分支，D11[固定变速及组合核心](fixed-retime.md)已实现。

## 已实现流程

1. 从有权登记的固定源版本建立独立扫描任务，读取真实视频时钟，完整解码所选文件，逐帧运行D04 YuNet；不使用D02稀疏帧冒充全视频检测，也不依赖OCR/CLIP标签成功。
2. 记录每帧源PTS、绝对/归一化时间、时长、显示坐标、置信分数、轨迹状态及解码帧哈希。默认所有检出轨迹纳入遮罩；不存在“自动识别医生”或人脸身份库。
3. 轨迹以相邻帧IoU一对一关联，采用当前帧检测框，避免平滑滞后；检出短暂丢失最多保守保持0.15秒，超过即停止猜框并记录风险。缩小图像差异检测切镜并清空旧轨迹。多人、快速移动、新轨迹、丢失和切镜均记录风险；IoU不能保证交叉后身份一致，排除轨迹须逐段检查。
4. 人工修订保存为不可变快照，可排除、裁剪时间、拆分、合并不重叠轨迹，以及增加带时间关键帧的手工框。坐标为经过旋转后的显示画面归一化xyxy；手工框在关键帧间线性插值，首尾必须覆盖明确区间。禁止未知轨迹、反向区间、无端点或非有限坐标。
5. 在源视频帧时间轴上渲染马赛克或实心黑遮挡，默认扩边25%，向外取整并裁至画面内。每帧记录实际遮罩区域和遮罩帧哈希，输出H.264/yuv420p/MP4。源文件不覆盖；变体记录源/输出哈希、轨迹修订、模式、强度、扩边、编码/模型/工具版本。
6. 重新探测输出全部帧，逐帧核对数量、PTS及显示时长，并完整解码。再由D09受信入口校验实际输出流、尺寸、共同时间基准、音轨和持续时间后登记私有衍生版本。任何失败都不登记可用输出。
7. 所有打码输出初始待复核。仅原输出所有者或有该对象read权限的指定复核人可进入私有连续预览；普通下载、常规预览和缩略图入口保持阻断。明确的人类复核事件绑定输出SHA256及完整recipe_hash后，才通过D17正常文件入口提供该固定版本。

检测/渲染成功不证明隐私已充分处理；人脸打码不处理姓名、病历号、声音等内容，也不提供许可或医学确认。D10不改正式表头/枚举，不自动写“含人脸=已处理副本”；后续D06依据实际复核状态处理正式字段。

## 时钟、资源与失败

扫描/手工关键帧以源视频流首时刻归一为0；保留原始PTS和time_base。渲染按D09选定视音频共同原点映射视频与音轨，保留原始偏移。VFR由每帧真实时间重建；最后一帧的持续时间显式设置，再重新逐帧校验。音频单独复用源时间轴编码，避免视频帧数上限截断更晚结束的音轨。D10当次固定1.0；D11已扩展0.25～4倍及同一源时钟的组合映射，默认仍1.0，重复请求不叠加。

默认最多60秒、1800帧、单帧1920²像素及累计10亿像素；所有上限同时适用，例如1080p/30fps受累计像素限制约16秒。扫描参数可在严格范围内提高至600秒/7200帧/4096²/80亿像素，但仍受Router默认300秒任务限时和每个工具120秒限制。超过预算明确失败，绝不截取前半段后宣称全片已检测。逐帧PNG会占磁盘，默认累计像素对应约3GB未压缩RGB数据/帧集合，扫描与渲染各自保留私有工作帧；提高预算前应准备相应容量。轨迹/风险报告限制各5万条，结果限制32MiB；日志运行中超过2MiB终止。

不支持的非方形像素比明确拒绝，避免强制setsar=1后拉伸。流声明结束时间早于实际末帧等时钟矛盾、帧数不符、尺寸动态变化、解码损坏、权重缺失/改变和失效扫描均明确失败。首轮以SDR样本验收，HDR、隔行和复杂业务画质尚未验收。

本机实现使用Windows Job Object，先将推理进程放入受控进程树，再开启启动门。取消/超时会终止整棵树并等待OS确认活动进程数为0，包括FFmpeg孙进程，再释放租约。状态查询不可用时继续持有资源，不把未知当已退出；执行器被强制结束后job句柄关闭也终止子树。此入口要求Windows，不以未测的其他平台静默替代。

任务结果、日志、轨迹和未处理帧仅在私有`jobs/`，不提供静态目录。成功扫描/渲染按源哈希、完整参数、模型/工具版本复用；失败后由D03重新规划相应阶段，旧epoch、取消或被替换的扫描不能成为修订依据。

## 启用与接口

在D03现有本地配置中显式增加以下字段（目录换成本机绝对路径）：

```json
"models": {"enabled": false, "model_root": "D:/ai/media-tag/cache/models/d04", "clip_device": "cuda"},
"redaction": {"enabled": true, "reviewers": []}
```

`models.enabled=false`仅关闭后台OCR/CLIP等标签阶段，D10仍可独立运行YuNet CPU。`redaction.enabled`默认false。复核人列表默认空，无默认批准者；项目负责人须指定真实actor_id，并配置editor/admin身份及该输出read授权，才能批准/撤回。Worker及普通reader不能复核，不能从请求体自报角色。尚未指定生产复核人，当前私有验证批次没有创建隐私批准记录。

```powershell
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
& .\.venv\Scripts\python.exe -m media_tag prepare-models
& .\.venv\Scripts\python.exe -m media_tag serve --config config/YOUR.local.json
# 在另一个终端，每次执行最多一个已排队阶段；此命令本身不启动常驻轮询。
& .\.venv\Scripts\python.exe -m media_tag work-once --config config/YOUR.local.json --worker-id YOUR_WORKER_ID
```

服务仅绑定loopback；身份、私有配置、模板与权重前提沿用D03/D04，示例中的YOUR值须替换为实际已配置值。

| 方法/路径 | 输入与输出 |
| --- | --- |
| `POST /redaction/scans` | ScanSubmission：request_id、parent_asset_id、expected_revision、可选options及chain_from_derived；返回独立job |
| `GET /redaction/scans/{job_id}` | 返回真实任务状态；成功且已停止时返回scan_task_id、lease_epoch、report_sha256和完整data，含轨迹/风险/源时钟，供修订客户端直接读取 |
| `POST /redaction/revisions` | RevisionSubmission：上述扫描三元组及edits；返回不可变revision_hash；无edits表示默认处理全部检出轨迹 |
| `GET /redaction/revisions/{revision_hash}` | 本人读取固定修订及原扫描绑定；扫描失效则拒绝 |
| `POST /redaction/renders` | RenderSubmission：request_id、revision_hash、mode(mosaic/solid)、strength(1～100)、padding(0～1)、audio、audio_stream_index、chain_from_derived；D11允许speed(默认1.0)及内部segments，仍拒绝任意FFmpeg参数 |
| `GET /redaction/renders/{run_id}` | 真实job状态、固定plan/revision、登记后的asset_id，以及pending/approved/revoked/output_unavailable隐私状态 |
| `GET/HEAD /redaction/outputs/{asset_id}/preview-content?sha256=…` | 指定输出哈希的私有复核预览，支持Range/ETag；不返回源视频；每次读取重查身份、对象权限、来源及生产租约 |
| `POST /redaction/outputs/{asset_id}/review` | ReviewSubmission：expected_revision、output_sha256、recipe_hash、decision(approved/revoked)、full_output_viewed、exclusions_checked、risk_intervals_checked、note；批准须三项显式为true |

所有redaction响应禁止共享缓存。批准时验证实际输出字节与当前生产者；撤回仅收紧权限，文件损坏/缺失或生产任务取消也不阻止指定人员按元数据哈希撤回。采用CAS并记录实际操作者、时间和绑定哈希。新轨迹/强度产生新配方及输出，旧复核不能用于新输出。撤回后已打开的普通下载流下一次读取也被拒绝。复核只允许该固定输出通过文件门槛，不自动共享ACL，不改变原件既有显式权限，也不替代当前剪辑用途检查。

手动补框的edits示例（诊断示意，时间必须落在实际源范围内）：

```json
{"action":"manual","new_track_id":"manual-1","start_seconds":0.0,"end_seconds":1.0,
 "keyframes":[{"seconds":0.0,"box":[0.1,0.1,0.3,0.4]},
              {"seconds":1.0,"box":[0.2,0.1,0.4,0.4]}]}
```

其余action：exclude使用一个track_ids；trim用一个轨迹与start/end_seconds；split用一个轨迹、start_seconds及new_track_id；merge用两个以上不重叠轨迹及new_track_id。合并/拆分不识别人，不自动推定两条轨迹属于同一人。

SQLite迁移4只新增修订/处理任务关联/复核事件表，不改旧迁移和不可变变体。输出采用在资源已退出后由本地受信回调执行，启动时可补做中断的采用；Worker角色不能调用发布/批准接口。

## 实际验证与交付边界

D10新增31项工程测试及2项并发路径回归测试，全量342项。包括实际双人脸完整检测、实心/马赛克渲染、手工补框后实际编码像素检查、轨迹排除/拆并/裁剪、VFR/旋转/非零源时间/音轨偏移、预算及非方形像素拒绝、迁移回滚、取消/超时/孙进程清理/查询失败、私有预览、逐对象授权、指定复核人、哈希/CAS、批准/撤回与损坏输出不回退原件。

真实六模板验证批次为`jobs/d10-validation-20261107-51e5d9e3/validation.json`（忽略Git）。12帧移动/遮挡/切镜合成视频实际逐帧扫描，记录4条轨迹并输出两种遮罩；loopback状态/报告/复核私有预览/Range和输出哈希通过，普通入口及未指定复核人的批准请求拒绝，原件哈希和revision不变，两个输出仍待复核。扫描报告耗时0.610秒，实心/马赛克渲染0.579/0.531秒（该640×360短样本，不含外层CLI启动，不作为负载/P95指标）。

尚缺A04要求的至少12条不同业务来源授权视频、目标人脸标注、冻结的自动遗漏/人工修正耗时门槛及指定人员全片复查。合成图的移动、镜像和遮挡不能代替真人侧脸/转头等业务风险集；工程通过不表示A04或无人复核使用已获准。D12合成人员页面操作已验证，医学管理/语义集成与业务签收仍待后续；最终Windows部署验收留D07。

实现依据：[FFmpeg concat格式](https://www.ffmpeg.org/ffmpeg-formats.html)、[帧时间选项](https://www.ffmpeg.org/ffmpeg-all.html)、[setts位流滤镜](https://ffmpeg.org/ffmpeg-bitstream-filters.html)、[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

最终普通wheel复测还触发既有Windows并发入库路径别名问题：CPython realpath在目录/文件同时创建时偶发保留`\\?\`前缀。存储层现统一等价的驱动器/UNC前缀表示，再执行原有reparse、边界和实际句柄检查；回归测试确认真实越界仍拒绝，修复后615轮双线程同文件登记通过，未放宽文件根权限。
