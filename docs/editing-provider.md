# D19 任务内剪辑取材适配

2026-11-08。已实现本仓库客户端工程；实际剪辑宿主集成仍待指定。实现位于 `src/media_tag/client/`，复用 [D17固定版本API](asset-search-api.md) 和 [D18认证客户端](mcp-adapter.md)。没有修改相邻仓库、共享素材、旧剪辑任务或其预览/导出策略。

## 实际范围

只读核对发现相邻剪辑仓库在本机实际为 `D:/ai/auto-video`，早期文档中的 `doctor-video-automation` 为原拟定项目名。已阅读其公开 AGENTS、PREVIEW_ONLY_WORKFLOW 与 prepare_media/media_library 实现，没有读取 `.env`。现有登记器不认识 `media-tag`，因此本轮不会把新来源伪装为 Servier、public 或 local-licensed，也不把内网入口塞进公共下载器。

`TaskProvider` 只使用显式配置的 reader 凭证和固定服务 origin，无代理、跨域跳转或任意 URL 下载。请求不包含服务端磁盘路径；只有实际下载、SHA256/大小/签名、技术信息及完整画面解码校验通过后才返回真实 `local_path`。

通过 `TaskStore(jobs_root, job_id)` 生成新任务，jobs_root 必须是绝对本地 `jobs` 目录。拒绝已有但没有本适配器所有权标记的任务；已标记任务仅复用本适配器自己的文件。检查 Windows 保留名称、穿越、符号链接/reparse、实际打开句柄与硬链接，进程级文件锁防止并发写同一哈希。

实际产物为 `jobs/<job_id>/edit/media-tag/` 内固定哈希媒体、`.part`、锁、不可变 receipt 和 preview-handoff JSON。任务根、edit及适配器目录维护README；来源、候选、稿件和检查原件由调用方保留。记录 provider=`media-tag`、素材/root/parent ID、文件哈希、metadata revision、检索版本、实际文件时钟、已处理配方/累计速度及来源/署名。许可保留未核实，医学状态 pending，获取时的授权门槛通过不代表人脸已匿名化。

## 下载与复用

每次获取和准备采用前都重新查询能力、固定版本清单及详情，完成下载/解码后再次检查授权和元数据。已下载字节不构成永久授权，宿主每次重新采用必须再次调用适配器，不能只凭旧JSON或磁盘文件绕过检查。权限撤回不会远程删除已经保存的字节。

默认最多512MiB、传输预算300秒（可用Python接口配置至2GiB/600秒）、socket超时来自客户端配置；逐块检查传输预算，网络失败保留部分文件。收到Range 206时逐项核对Content-Range、长度、强ETag、Content-Type及不压缩；服务返回200时清空本任务部分文件，从同一固定版本重下。完整哈希不符清理损坏part后报错，下次可重试。正式文件不覆盖，损坏正式缓存明确失败。

默认只接收PNG/JPEG/WebP/GIF/MP4，使用已有固定FFmpeg和Pillow，限制像素与解码耗时，视频最长600秒；超出预算明确拒绝。本轮是画面取材接口，不隐式转码其他格式。未知/未就绪技术元数据、当前文件时钟或源片段缺失均明确报错。

视频区间是实际下载文件的归一化时钟，必须在真实时长内，并与成片窗口等长；未实现本地再次变速，已登记配方保持 `apply_transform_again=false`，不继承剪辑项目默认1.25倍。额外剪切/变速由后续宿主显式处理并重新绑定检查。

## 当前用途交接

`Acquisition` 包含固定 asset_id、sha256、UseContext及可选search_versions。UseContext记录unit、quote、purpose、成片窗口、视频源区间、稿件和candidate的任务相对路径及完整SHA256。下载receipt的current_use_review始终是not_checked。

`prepare_preview(CurrentUseCheck)` 要求明确pass、实际观看声明、真实操作者标识/带时区非未来时间、相同receipt/文件/用途、十项检查及证据文件。检查内容为 semantic/action/timing/composition/packaging/technical/style/source/license/eye_mosaic；语义/来源/技术不能豁免，只有license可在显式关闭该项检查时skipped。适配器核对本任务内稿件、candidate和证据的实际哈希，重新获取授权及字节。任何用途、区间、文档、证据、元数据或权限变化使旧检查失效。

检查真实性由调用者负责，适配器明确记录 `caller_attestation_not_verified_by_adapter`，不会从一个布尔值推断模型确实看过素材。测试中的合成检查单不算AI实际观看或业务验收。产物是待宿主消费的preview handoff，固定preview_only=true、medical_status=pending、final_export_allowed=false、host_registration_performed=false。它没有调用相邻 accept_materials/check_edit/render，不替代该项目当前用途验收、包装校验或最终医学审核。

## 已可运行命令

使用私有JSON及reader-token文件，格式沿用 [客户端示例](../config/library-client.example.json)。input应为对应严格模型；可通过 `Acquisition.model_json_schema()` / `CurrentUseCheck.model_json_schema()` 获取结构，不传任意下载地址。

```powershell
& .\.venv\Scripts\python.exe -m media_tag acquire-media --config D:/APPROVED/client.local.json --jobs-root D:/APPROVED/jobs --job-id NEW_JOB --input D:/APPROVED/acquisition.json
& .\.venv\Scripts\python.exe -m media_tag prepare-preview-handoff --config D:/APPROVED/client.local.json --jobs-root D:/APPROVED/jobs --job-id NEW_JOB --input D:/APPROVED/check.json
```

以上入口已实现，路径为占位。不能指向相邻现有任务；实际跨仓库维护和宿主调用位置仍待明确。许可关闭参数 `--license-validation disabled` 只适用于已经获准使用该策略的宿主，不自动修改任何项目配置。没有合格用途检查时保留缺口或换候选，不能自动pass。

## 验证与待验收

项目`.venv`全量609项、最终目标13项、27包核心wheel环境26项及44包MCP环境24项通过；pip check/compileall、80份Markdown和126个wheel模块/资源一致性通过。13项新增测试覆盖真实鉴权API/媒体探测解码、断点恢复及忽略Range重下、损坏/错误头、缓存撤权、用途/稿件/证据变更、未观看/拒绝/许可开关、旧任务/路径/硬链接/锁和CLI诊断脱敏。

真实本机批次 `jobs/d19-provider-68e8f4c9/validation.json` 完成实际BGE CPU、变速副本单独授权、网络中断后Range恢复、重复字节复用、CLI独立任务下载与缓存撤权。实际81次HTTP、9次CPU任务、从1366字节恢复；原件及六模板不变，无医学有效值/隐私批准/整行确认。实际AI宿主、实际看片和剪辑端完整预览未验收，D19整体保持待集成，后续见 [总进度](PROGRESS.md)。
