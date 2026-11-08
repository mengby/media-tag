# D07 离线备份还原工程

2026-11-08。已实现离线备份、完整校验和隔离还原CLI；最终Windows部署/容量、常驻服务、网络、真实人员与业务验收仍待条件。实现 `src/media_tag/maintenance/backup.py`，测试 `tests/maintenance/`。沿用项目`.venv`，无新依赖、无数据库迁移，也未修改服务启动行为。

## 备份范围与一致性

备份包含完整SQLite状态、全部已登记blob的本地CAS副本，以及workspace/jobs内任务结果/证据/词表发布和文本索引文件。来源文件只读，不复制来源盘或相邻共享assets；模板、模型权重及本地运行配置/原始凭证文件不进入备份。数据库保留权限、身份、可能的会话哈希及其他敏感业务记录，备份必须放在受控私有目录并配置操作系统访问权限；不能提交Git或公开分享。

操作者先停止本服务及所有Worker/上传操作，再显式传 `--offline-confirmed`。工具不会替操作者停进程，不以该参数冒充进程状态证明。它拒绝active leased/running任务、资源hold及validating上传；未结束的租约先按现有队列恢复流程处置，不直接改表清空。receiving上传可离线保存部分字节与数据库偏移，但备份期间不得继续写入。

使用单独连接持有BEGIN IMMEDIATE写保留锁，再通过只读连接调用SQLite backup API生成数据库副本；不直接复制活跃主库文件或随意拼接WAL。该做法结合停服约束与复制前后文件核对，保持数据库和文件对应关系；[SQLite备份API](https://www.sqlite.org/backup.html)及[锁限制](https://www.sqlite.org/c3ref/backup_finish.html)为实现依据。在线运行的跨文件快照不在本轮范围。

检查全部9版迁移摘要、integrity_check、foreign_key_check及各表行数。逐个核对数据库中的blob、已采用Worker产物、已发布词表及文本索引的实际文件哈希；缺失/损坏不能生成有效备份。workspace目录不允许linked/reparse成员，发现.env、.local或credential/token/secret命名的文件明确拒绝，避免把配置凭证混在任务树中。不要为通过检查删除业务文件，应先核对其归属与安全保存位置。

清单绑定每个成员路径、SHA256、字节数、模板指纹与数据库表行数。固定文件数最多100000、默认数据预算50GiB，并检查所需空间另加64MiB余量；大库可通过Python接口显式调整字节预算。源workspace复制前后清单/哈希变化则失败，清单最后发布；失败目录不算备份，不影响上一次有效备份。禁止覆盖已有目标，使用新的备份目录。

## 已可运行命令

以下入口已实现，路径和HASH为占位；没有运行这些占位部署操作。

```powershell
& .\.venv\Scripts\python.exe -m media_tag backup-offline --config D:/APPROVED/runtime.local.json --destination D:/PRIVATE_BACKUPS/NEW_SNAPSHOT --offline-confirmed
& .\.venv\Scripts\python.exe -m media_tag verify-backup --source D:/PRIVATE_BACKUPS/NEW_SNAPSHOT --manifest-sha256 HASH_FROM_BACKUP_OUTPUT
& .\.venv\Scripts\python.exe -m media_tag restore-offline --source D:/PRIVATE_BACKUPS/NEW_SNAPSHOT --destination D:/APPROVED/NEW_ISOLATED_ROOT --manifest-sha256 HASH_FROM_BACKUP_OUTPUT
```

备份输出的manifest_sha256应保存在独立受控记录中。verify/restore先核对该摘要，再按清单校验所有文件及数据库引用；清单摘要证明一致性，不替代备份来源真实性或访问权限。只恢复到全新本地目录，不回写原库、旧任务、模板或共享存储；没有递归删除、自动清理或覆盖开关。损坏/不完整备份、错误清单/后端、路径链接和容量不足均明确失败。

还原产物为database.sqlite3、objects/、workspace/及restore.json。恢复后不会自动启动服务、设置账号、防火墙或部署MCP。另行准备本地私有配置：将database/workspace/object_store改为还原路径，保留真实actor ID与审核角色，凭证从可信凭证存储重新配置；同时核对tool_dir、model_root和review.taxonomy_manifest等绝对路径，不能直接沿用旧机路径。六原始模板需另行授权提供并匹配备份模板指纹；本地模型需按原固定版本校验，不能用其他权重替代已发布文本索引。

## 目标部署仍属拟定

最终机器未指定，当前`.venv`的基础解释器来自Codex缓存，不能复制它当作服务器运行时。目标机器需独立维护Python3.12、按对应哈希锁重建`.venv`、验证固定FFmpeg及已启用模型/CUDA；创建受控数据/备份目录并明确账号、容量、保留周期及还原目标。核心/视觉/文字/MCP依赖锁和兼容证据见 [环境说明](DEVELOPMENT_ENVIRONMENT.md)。

已有`serve --config`只监听loopback；跨机器访问、TLS、反向代理、网络策略与常驻API/Worker启动方式须结合实际机器部署。本轮没有开放端口、安装计划任务/系统服务或改用户网络配置。备份工具可随部署使用，但本机合成还原不等于最终部署及A08/A10综合签收。

## 实际验证

已验证空库、完整WAL数据库、人工值/权限/版本保留、固定清单、损坏/缺失、活动租约、秘密文件命名、源变更、Windows真实junction及CLI脱敏。真实工程批次以停服后的D20合成库为只读输入，通过三个独立CLI命令备份/校验/还原到新目录，再实际启动隔离HTTP，核对素材/人工值/权限/衍生关系/词表整组状态及BGE CPU、下载哈希和访问拒绝。

项目`.venv`全量621项、最终维护13项、27包纯核心wheel环境30项通过；最终维护测试额外覆盖有实际未checkpoint WAL页面的完整备份及空库恢复。私有批次为 `jobs/d07-recovery-7e7d62dc/validation.json`：106文件/687275字节，三个真实CLI、9次恢复后HTTP及BGE通过；详细检查记录见 [进度](PROGRESS.md)。没有生产素材、实际审核人或跨机器服务器签收；D07整体仍待部署验收。用户暂缓的正式词表/独立集/审核角色、业务视频、最终主机与AI宿主条件仍待后续，D05可选VLM和D08飞书/OSS不在本次首期必做范围。
