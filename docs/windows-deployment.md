# D07 当前电脑部署

2026-11-08：用户已确定先部署在当前电脑，使用本项目`.venv`。本机部署/停止重启/受控进程恢复工程已验证；业务综合验收、实际注销登录/重启电脑、Mac/其他机器访问仍未签收。

## 实际安装

| 项目 | 当前值 |
| --- | --- |
| 运行账号 | 当前Windows登录用户EDY，SID尾号1001；任务RunLevel=Limited、Interactive登录 |
| 系统 | Windows11专业版10.0.26300 x64；本机RTX5060 Ti，实际CUDA矩阵和固定模型复查通过 |
| 页面 | http://127.0.0.1:8765/ui/ |
| 运行解释器 | `D:/ai/media-tag/.venv/Scripts/pythonw.exe`监管；子进程使用同项目python.exe，无额外虚拟环境 |
| 登录启动任务 | `MediaTag-Local-6cd832eafba7`，EDY登录触发；失败有限重试，忽略重复实例 |
| 私有数据 | `D:/ai/media-tag/data/local/`：catalog.sqlite3、objects、workspace、logs、backups、restore-tests |
| 运行配置 | 上述目录的runtime.local.json、instance.json；初始化不会覆盖既有实例或轮换已有凭证 |
| 人员登录 | 双击桌面“素材处理”自动建立本人会话；长期凭证仅留私有目录，不手输、不写聊天或Git |
| AI身份 | 独立reader凭证与reader-client.local.json，仅取材，无上传、修改、审核或发布权限 |

私有目录已修正为实际EDY所有、只授权EDY及SYSTEM，保护继承；不是Codex隔离账户。数据库、媒体、模型缓存、备份、日志、令牌和本机配置继续Git忽略。原六模板与来源文件只读，没有导入正式医学词表、候选数据或业务素材，也未写入相邻`D:/ai/auto-video`。

已启用上传/文件、人员页、人工字段机制、术语管理机制、BGE服务能力、OCR/人脸/CLIP专用Worker、打码及固定变速。当前没有正式发布的词表，语义查询明确降级，CLIP报告partial/unpublished_vocabulary并待处理；这不是医学标签通过。实际医学审核人与输出复核人尚未指定，相关角色列表保持空。当前本机库为空，工程素材留在独立验证实例。

## 已可运行操作

从本仓库的普通EDY PowerShell运行；运行私有目录受ACL保护，隔离账户不能代替实际用户读取凭证。首次初始化也应在实际登录用户下执行。

```powershell
& .\.venv\Scripts\python.exe -m media_tag local-status --root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Status -Root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Stop -Root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Start -Root D:\ai\media-tag\data\local
```

`Install`为已初始化实例安装当前用户登录任务，重复执行不改变既有任务；若同名动作/说明不匹配则拒绝修改。`Remove`只移除本实例启动任务并先正常停止，不删除数据库、媒体、备份或凭证。没有创建应用提醒或云端自动化。

正常停止通过实例代次匹配的本地控制文件，先等当前阶段退出；超出宽限后由本实例Job Object终止子树。不要手动使用本实例worker凭证启动额外执行器，租约恢复的可信进程边界是这个监管器管理的专用Worker。

## 进程与恢复依据

每个实例只有一个持有实际文件锁的监管进程，管理API和Worker两个子树。子进程以CREATE_SUSPENDED创建，先加入本实例命名Windows Job Object，再通过ResumeThread恢复主线程和stdin门闩启动应用；Windows`.venv`启动器派生的实际解释器因此也在同一Job内。创建挂起线程和恢复时序依据 [Microsoft进程创建标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) 与 [线程挂起说明](https://learn.microsoft.com/en-us/windows/win32/procthread/suspending-thread-execution)。

监管器异常退出时Job关闭终止后代。新监管器先重新打开同名Job并等待OS确认无存活成员，再仅对本实例Worker尚未释放的尝试调用可信confirm_stopped；不按心跳超时推断GPU已释放，不对其他Worker签发停止确认。只重试中断阶段，已成功阶段按原指纹复用；Worker重启次数及计划任务重试均有限，错误码和代次进入512KiB轮转日志，不记录令牌/源文件正文。

本机GPU已复查：torch2.9.1+cu128、CUDA12.8、RTX5060 Ti/sm_120，实际矩阵最大绝对误差0.000335693359375，原固定D04权重摘要通过。该检查是工程兼容证据，标签效果与隐私质量仍须真实业务集和人员复核。

## 备份与回退

正常停止后使用 [离线维护CLI](offline-recovery.md)，备份保存到私有`data/local/backups/<新批次>`，恢复检查保存到私有`restore-tests/<新批次>`，不覆盖原库。运行目录内这些位置均继承EDY/SYSTEM ACL；同盘备份/还原验证不等于独立磁盘灾备。当前不自动清理或定时删除备份，保留周期与恢复目标仍待用户确定。

恢复产物的完整哈希、数据库/对象/证据关系先校验，再准备目标机器/新实例的运行配置与凭证；不能把验证目录直接当作已切换的生产实例。现有`.venv`依赖的基础解释器仍在Codex缓存中，目标机器或基础解释器迁移时按锁重建环境；本轮按用户要求沿用项目环境。

## 实际验证与未完成项

`tests/deployment/test_local.py`最终8项覆盖空库初始化/幂等/凭证、目录/端口及实例归属、真实互斥锁/停止代次、启动门闩、实际解释器命名Job归属和Job分配失败后的挂起进程清理。项目`.venv`全量629项、最终部署8项通过，纯核心普通wheel环境53项、pip check/compileall与90份Markdown/130模块资源一致性通过。初始化与状态读取共用短时锁，重启不会把旧代次误报成新实例已就绪。

真实独立实例批次 `jobs/d07-local-3ade147c/validation.json`：9次HTTP、真实CPU/OCR/人脸/CLIP CUDA；在GPU租约持有期间杀掉监管器，8个旧后代全部退出后重启，仅中断CLIP重试，旧租约释放及配额清空。CLIP明确记录词表未发布，不伪造标签成功；固定下载/只读拒写/46文件605791字节离线备份还原通过。原图片哈希不变，正式发布、医学/隐私批准均为0。

实际EDY登录任务另见 `jobs/d07-installed-b4e0cd42/validation.json`：启动/停止/重启、1个数据库文件备份/还原，以及真实Codex全局MCP入口的停服错误/恢复后空库查询通过。私有备份路径及摘要见同批次private-backup.json；最后保持服务运行。没有注销当前Windows用户、重启整机、开放防火墙或外网监听；真实登录触发与跨机器、A04/A05/A06/D16及完整I类业务签收仍待后续。

本机人员自动登录已启用，具体使用/安全边界/首次迁移与实际验证见[说明](local-browser-login.md)。普通地址在没有会话时提示使用桌面入口，不自动授予匿名编辑权限。
