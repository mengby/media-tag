# E 盘入库素材的打标前清洗

本机从 `E:\baidudownload` 或其子目录执行 `intake scan`、`intake apply` 时，客户端在新的素材库上传请求中登记 `pretag_cleaning=true`。选源按路径组件匹配，来源范围在 `media_tag/intake/pretag-roots.json`；`F:\图片` 不在该范围。服务只读取已授权上传的原件。启动服务本身不会遍历源盘。每次使用本次 E 盘扫描返回的批次目录，人员页原有的全库打标按钮仍按当前账号的全部素材选择。

1. 盘点把 `.swf` 单列为 `deferred_swf`，把“论文”目录下的文件和文件名含“论文”的文件列为 `deferred_paper`。两类都保留原件并跳过打标、上传；两者重合时归入 SWF。其他入选图片、视频及动画按原有 SHA256、真实格式校验及重复来源登记进入持久化作业。无法解码、实际容器不符等仍由原入库错误记录处理。
2. 探测与抽帧提供清洗和打标共用的证据。普通视频尝试头部、50% 和 90% 三处，记录实际解码时间；短片尾部无画面时记录缺失位置。图片按整幅画面取样，动画沿用有界证据。清晰度指标计算在抽样图上，当前没有自动淘汰阈值。
3. 配置 OCR 时复用该任务已生成的文字、坐标和帧哈希。本机 Qwen 可用时在受 GPU 租约管理的离线子进程中补查角标、署名与图形水印。清洗报告保存命中和不确定状态；缺少某项能力会标明。疑似水印进入复核，语义打标仍可继续。技术失败阻止依赖阶段，显式重试可复用成功上游。不删除、遮挡、转码或覆盖原件。
4. 打开人员页素材详情查看“打标前清洗”。`GET /v1/assets/{asset_id}/pretag-cleaning` 返回当前状态、任务 ID 和可用报告。证据图通过 `GET /v1/assets/{asset_id}/pretag-cleaning/frames/{frame_sha}?task_id={task_id}` 读取，重验素材访问资格、当前任务和帧哈希。旧报告不能替代当前阶段。

准备好正式开跑时，在项目根目录按 E 盘子目录开批，使用各批实际的 ID。下面这个来源目录在前次盘点中有 339 张图，符合现有批量打标入口每批最多 500 条的限制。

```powershell
& .\.venv\Scripts\python.exe -m media_tag intake scan --source 'E:\baidudownload\3500像素白底339张' --jobs-root 'D:\ai\media-tag\jobs'
$batch = 'D:\ai\media-tag\jobs\<本次 scan 输出的批次 ID>'
& .\.venv\Scripts\python.exe -m media_tag intake apply --batch $batch --local-root 'D:\ai\media-tag\data\local'
& .\.venv\Scripts\python.exe -m media_tag intake tag --batch $batch --local-root 'D:\ai\media-tag\data\local'
```

首次启动整批前需要按本机实际上传配额、可用空间和队列速度分批；既有的上传、像素和单文件上限继续生效。当前集成限于本地服务。OSS 与 MySQL 留待后续阶段。

2026-11-16 在隔离实例验证了 E 盘的 1 张图片和 1 条医学视频。两个样本均完成探测、取证、OCR、水印初筛及画面字段打标，并核验了原件哈希。视频在 58.4 秒命中版权字样，在 105.1 秒命中版权字样和 “SAMPLE USE ONLY”。该试跑验证了服务流程；400 GB 全量十字段打标的耗时仍待分批实测。
