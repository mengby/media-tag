# D02 技术提取与证据

日期：2026-11-07。状态：技术分析Python接口和环境检查命令已实现，已用真实工具解码合成素材。D03已增加[调度HTTP、队列和CPU执行适配](router-queue.md)，D04另有[真实模型适配](model-workers.md)；无常驻部署、医学审核或全量素材处理。对应D02、R01/R04/R07/R08/R10、N01/N03/N04/N05；A02/A06仅技术工程前置，内容与业务验收仍未执行。

## 本机环境：实际检查

最初PATH及已检查的工具目录未找到FFmpeg/ffprobe。本轮将便携工具放入Git忽略的 `cache/media-tools/ffmpeg-9.0.2/bin/`，没有更改系统PATH或安装全局服务。

| 工具 | 实测版本/摘要 |
| --- | --- |
| FFmpeg | `9.0.2-essentials_build-www.gyan.dev`；SHA256 `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec` |
| ffprobe | `9.0.2-essentials_build-www.gyan.dev`；SHA256 `f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52` |
| Pillow | `13.0.0`，安装在项目`.venv`；D02时开发锁共15个包，D03扩展为27个包 |

固定下载来源：[Gyan 9.0.2 essentials zip](https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip)，发布方[摘要](https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256)为 `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`。本轮下载后核对archive及两个exe摘要，缓存保留包内LICENSE/README，二进制不进Git。[工具锁](../src/media_tag/workers/media-tools.lock.json)只保存来源、版本和摘要。

新环境须另行获取该包、核对锁中的archive摘要后，把bin及许可证放在上述缓存布局；不会在导入Python包或运行分析时自动联网下载。也可显式配置 `MEDIA_TAG_TOOL_DIR` 或给 `Toolchain(directory)` 指定另一对工具，实际版本/二进制摘要始终写入阶段指纹。默认路径严格核对当前Windows锁；显式自定义工具需自行重新验证所用格式/选项，不能据版本号假定兼容。

在仓库根目录运行：

```powershell
& .\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements-dev.lock
& .\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
& .\.venv\Scripts\python.exe -m media_tag check-media-tools
& .\.venv\Scripts\media-tag.exe check-media-tools
```

检查返回实际版本与SHA256，缺少工具/默认锁不符则退出码2，保留结构化错误，不静默换用系统版本。当前验证走CPU，未验证GPU解码/编码、Windows服务或最终服务器的兼容性。

## 已实现接口与输入边界

- `Toolchain`：一对外部工具的定位/版本/摘要；命令用参数数组、无shell，关闭stdin；限制时间及stdout/stderr字节并及时终止进程。
- `probe_media` / `ProbeOptions`：技术信息、图像透明像素、独立音轨检查。
- `MediaAnalyzer.analyze`：通过D01 `get_asset/open_content`重新鉴权并核对固定版本，复制到私有job输入后执行probe/evidence两个阶段。
- `evidence_records`：把成功的实际图像/帧转为D00 `Evidence`及`ArtifactRef`，保留源SHA256；稀疏帧不转成连续视频片段证据。

底层探测函数只供受信Worker调用；用户入口应调用`MediaAnalyzer`，不能接收任意URL、盘符或文件路径替代授权素材ID。D01数据/权限不另做一套，实际源文件只通过已授权的只读流读取。新字段和技术证据保存在job记录中，本轮不向正式表头加列、不写人工有效值，也不把后台成功改成许可/标签已确认。

初期允许的常见文件demuxer包含MOV/MP4、Matroska/WebM、AVI、MPEG/TS、WAV/MP3/FLAC/OGG/AAC及常见图像；不放行网络、设备、播放列表、concat或脚本demuxer，只允许file/pipe协议。图像Pillow入口限PNG/JPEG/GIF/WebP/BMP/TIFF。文件名不参与类型判断或医学标签生成。白名单不表示所有编码/动画变体均已验收；本轮实测组合见下文，其他组合失败时如实记录。

## 技术事实与未知值

记录源SHA256/大小、实际容器/codec、视觉/音轨索引、尺寸、旋转、原始帧率/time base及起始时间等。原始ffprobe只请求必要技术项，旋转tag只保留有限数值，不保存任意媒体标题/完整tags或工具stderr。图像只保留格式、有限EXIF方向和已核实的帧数；不保存GPS等完整EXIF。

| 情况 | 输出行为 |
| --- | --- |
| 静态图片 | 时长为null，不把FFmpeg构造的图片时基当作素材时长；EXIF方向在元信息中保留，预览副本单独转正 |
| 视频旋转 | 保留display matrix/rotate数值；`display_width/height`是方向修正后的像素网格，SAR另在原始技术项保留；PNG预览归一为方形像素 |
| 透明像素 | 读取RGBA实际alpha，发现任何透明像素可为true；检查完整且全不透明才false；帧/总像素预算截断则为null，附实际检查范围 |
| 动画 | 逐帧检查受预算限制，不调用无界全帧计数；若未到EOF，不编造总帧数或“全部不透明” |
| 有音轨 | 与是否静音分开；封面attached_pic不冒充视频流，多个音轨分别保留 |
| 静音 | 按原采样率/声道解码float PCM，记录峰值、阈值与扫描秒数；超过阈值可确认非整轨静音，短窗口静音不能确认整轨静音 |
| 起点/PTS缺失 | 保持未知，抽帧阶段明确失败/整体partial；不填零、不按平均帧率补时间，不用best-effort估计冒充原生PTS |

视频透明性默认未知，PNG样本只表示该帧实际alpha；不能因样本不透明就填整片不透明。静音默认阈值-60dB，完整性要求已知音轨时长、扫描覆盖及样本数量吻合；无音轨时静音概念不适用，不写“有原声=false”冒充静音判断。

## 抽帧、时间与预算

ffprobe在包读取预算内取得实际帧PTS；在采样窗口内选择已有候选的首/中/尾，FFmpeg用确定帧索引选择、自动转正并输出PNG。再读取showinfo的整数PTS和time base，与源原生PTS核对；保存源PTS tick、解码时钟、源秒数与相对文件起点秒数。变帧率及非零起点均有真实解码测试，不靠`frame_index/fps`推算。

| 默认预算 | 值与含义 |
| --- | --- |
| 输入字节 | 2GiB，超过即在输入前置检查拒绝；图像压缩文件另限64MiB |
| 解码像素 | 单幅/帧16M像素；alpha检查累计32M像素、最多32帧 |
| 子进程时间/输出 | 单次20秒；stdout通常8MiB、stderr1MiB；超限终止，非完整OS内存沙箱 |
| 音频 | 最多20秒、64MiB PCM、最多8声道；未知布局保留原因，超窗不宣称全轨静音 |
| 帧索引/证据 | 最多读600个包，采样窗口从首个有效候选起30秒内，最多输出3帧；包数不是帧数，索引解码范围可能大于采样窗口 |
| PNG预览 | 最大边640，视频/图片均不主动放大；保留源哈希、变换版本及结果哈希 |

所有数值是当前工程起点，可按配置收紧，尚未作为团队容量或业务性能承诺。报告明确 `continuous_observation=false`、`full_video_observed=false` 和样本时间点；样本之间/窗口外仍未观察。场景切换召回和连续过程证据尚未实现，不能据此判断完整变化方向、整片无文字或无人脸；人脸完整轨迹/处理仍属D10。

## 阶段、复用和错误

实际产物在 `jobs/<job_id>/inputs/` 与 `runs/<stage-fingerprint>/attempt-<n>/`。job_id限受控标识，目录拒绝链接/重解析点；每个工作层有README。输入副本复用D01文件后端，不另建存储系统；所有输入、预览及stage JSON是私有工作数据，不进入已处理副本或共享静态目录。

指纹包含源哈希、asset/actor、阶段版本、工具二进制摘要/Pillow版本及该阶段配置。仅改抽帧配置可复用probe；metadata revision变更但文件未变时可复用技术证据，返回结果另带当前revision，D03采用前仍须验证其任务版本。阶段算法变化必须更新阶段版本。

每次尝试保存stage JSON、其摘要、实际输出哈希、配置/工具/版本、输入、错误及耗时。复用前重新鉴权、校验输入、manifest及产物；缓存拒用原因独立记录，失败/partial阶段不算成功缓存。未完成或损坏结果以新attempt重跑，成功阶段不覆盖；同attempt目录以原子创建认领，重复执行拒绝。D02不自动循环重试，调用方须遵守D00/D03总尝试预算；这些文件不是独立的运行任务队列。

前置权限、登记文件损坏、输入字节超限等直接抛出明确异常，不伪装无匹配。已进入阶段的格式/解码/像素/超时错误写入failed；probe失败则evidence为blocked。音轨执行失败为partial；纯音频无视觉证据为not_applicable；仅因覆盖预算不足的字段为null并附原因。当前`success`只表示规定范围的技术操作完成，不代表全文件完全解码、内容正确或审核通过。

## 验证与限制

新增33项D02测试，加D00/D01共149项。测试实际生成并解码PNG/JPEG/GIF、H.264 MP4（旋转/VFR/非零起点/80秒片段）、AAC、PCM WAV、多音轨MKA及带封面的MP3；故障注入明确标为合成测试。缺工具会失败，不以skip代替环境验收。覆盖权限先验、源/私有副本完整性、stage复用及损坏重跑、静音覆盖不足、原生PTS/起点缺失、进程预算与路径重定向。

真实模板绑定的本地合成验证批次：`jobs/d02-validation-20261107-adf2da79/validation.json`。生成3秒测试图视频＋静音AAC及透明PNG，完成登记→探测→实际帧证据→复用；原始字节和素材元数据未变。此目录及SQLite/媒体/报告全部忽略Git，新克隆不保证存在。没有读取生产素材或相邻项目，也没有模型调用、许可确认或医学审核。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/evidence -q
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m pip check
```

依据：[FFmpeg主项目命令文档](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/doc/ffmpeg.texi)、[ffprobe主项目文档](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/doc/ffprobe.texi)、[Pillow Image文档](https://pillow.readthedocs.io/en/stable/reference/Image.html)。当前仅Windows本机工程验证；队列/租约的工程验证见D03，格式容量冻结、真实样本、服务器部署与最终业务验收留后续任务。
