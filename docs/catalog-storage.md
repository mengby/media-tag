# D01 索引与存储核心

日期：2026-11-07。状态：Python核心库已实现并完成工程验证；D03已增加[调度HTTP与队列](router-queue.md)，没有生产素材导入或常驻服务器部署。使用项目`.venv`，无新增第三方依赖。对应D01、R01/R05/R09、N01/N02/N03/N05/N06，覆盖A09/A10的核心前置，不代表完整业务验收。

## 实体与身份

SQLite由受信服务访问，数据库文件须在本机磁盘；拒绝UNC和网络映射盘，不通过SMB共享数据库。D01迁移为版本1，D03追加版本2队列表；D09再追加版本3上传/衍生契约，当前迁移版本为3，迁移记录含实现摘要；未知版本、非空且未版本化的数据库及迁移摘要不符均拒绝。迁移逐条SQL执行并处于同一事务，失败回滚DDL与版本号；所有连接开启外键。

| 实体 | 已实现行为 |
| --- | --- |
| `blobs` | 小写SHA256唯一，保存大小；同内容复用，数据库触发器禁止修改内容身份 |
| `assets` | UUID派生的稳定ID，绑定不可变blob和owner；原件root指向自身；metadata revision独立增长 |
| `asset_sources` | 独立来源ID、逻辑来源后端ID、原始相对key、人工提供的出处/署名及记录人/时间；默认许可未核实 |
| `storage_locations` | blob到后端ID/对象key的映射；实际根目录与凭证不存数据库 |
| `asset_aliases` | 按owner及namespace保留旧ID；同一查询若命中多个已授权素材，要求使用固定素材ID |
| `registrations` | owner＋request_id幂等；同请求不同输入拒绝，不按同名文件或SHA256自动合并业务身份 |
| `asset_permissions` | 素材级read/edit/suggest授权；默认仅owner可访问，管理员是显式受信角色 |
| `effective_values` / `tag_suggestions` | 人工有效文本与AI/工具建议分表；建议不更新人工值或整条素材确认状态 |
| `suggestion_runs` | 保存完整D00任务/结果及操作者，保留输入/规则/模型/词表版本、证据、错误和资源记录；不是任务队列 |
| `field_revisions` | 修订版本、操作者、原因、前后JSON及服务端记录时间，随变更事务提交 |

新素材状态固定为内部`ai_draft`，来源许可为`unverified`，不设置假审核人。即使字节相同，不同来源/账号的登记也各自保留asset/source与权限，只有blob复用。不同原件组不因SHA256相同而合并授权或root关系。重复同一请求则返回已有asset；来源路径迁移不改变素材ID。要另添来源应使用新的登记请求，不能借重试改写第一次的追溯记录。

原件/衍生关系字段已预留，但D01公开登记只接原件；衍生配方、输出审核与版本入库由D09～D12实现。D01读取入口拒绝所有derived对象，避免绕过尚未实现的复核流程。

## 本地文件后端

`FilesystemBackend`使用已存在的受控根目录。原件后端只读，对象后端须明确可写，两个根目录不得相同或互相包含。后端名称与根目录由服务配置绑定；`source_backend_id`是来源逻辑ID，登记时必须显式提供，不从盘符猜来源。

副本流程：打开只读源句柄→流式复制并计算SHA256/大小→刷新临时文件→以哈希生成对象key→同卷硬链接原子发布自有临时副本→重新校验目标→删除自有临时文件。**不对原件建立硬链接**，也不移动、覆盖、重命名或删除原件。已存在对象必须通过大小/哈希检查才复用，损坏时不覆盖。复制期间发现源大小/mtime/文件身份变化则拒绝登记。

拒绝绝对路径、盘符、URL、`..`、Windows设备名和链接/重解析路径组件。Windows读取在打开后检查实际文件句柄的最终路径，防止只检查打开前的文件名。Linux使用可用的`/proc/self/fd`，其他平台检查文件身份；当前仅Windows实测。根目录需由受控服务账号管理，这些检查不替代操作系统权限或提供对恶意本地管理员的隔离。

对象存储要求支持同卷硬链接的本地文件系统；当前Windows环境已验证。不支持时明确失败，不退回会暴露半写入文件的发布方式。`OssBackend`是明确未配置的占位，所有I/O报`BackendNotConfigured`，没有SDK、凭证或真实OSS调用。

## 事务、快照与失败边界

数据库写操作使用`BEGIN IMMEDIATE`；读快照用同一读事务，并使用WAL。登记的blob/location/asset/source/alias/幂等记录/初始审计同事务提交。修订、权限变化和归档也与审计同事务提交；错误不留下半条元数据。

文件对象先于数据库事务完成。后续数据库失败可能留下完整、未引用的哈希对象：它没有可查询的asset，不能通过素材入口获取；重试可重新校验复用。D01不会擅自删除该对象，以免删除另一个并发事务使用的副本；清理须另做核对引用的维护任务。文件系统与SQLite不是跨资源原子事务，断电恢复和真实备份还原仍属于D07。

每次metadata/ACL变更都校验调用者的`expected_revision`。两个并发修改只有一个能按旧revision成功，另一个得到`RevisionConflict`，不得最后写入覆盖。`snapshot`返回一次读事务内、仅含可见素材的脱离数据库快照；不等于数据库备份或正式导出发布。

依据：[Python sqlite3事务说明](https://docs.python.org/3.12/library/sqlite3.html#transaction-control)、[SQLite显式事务](https://www.sqlite.org/lang_transaction.html)。本轮使用原生sqlite3，未引入ORM。

## 授权与修订入口

`Principal`只能由受信服务在认证后构造，不能直接相信网页/MCP传来的actor_id或role。D01未实现登录、账号管理、TLS或HTTP路由。底层`Database`/存储对象仅供受信服务调用，不能直接交给远程客户端。

| 公共方法 | 行为 |
| --- | --- |
| `register_file` | editor/admin登记只读来源，默认私有，返回固定asset快照 |
| `get_asset` / `snapshot` / `resolve_alias` / `history` | 每次检查owner/admin或对象授权，不提供跨账号的哈希存在性查询 |
| `change_access` | 仅owner/admin变更read/edit/suggest，需revision和原因；edit包含read，suggest不包含read |
| `revise` | editor/admin且具备edit；可改显示名，或事务采用带证据、操作者和版本的非医学文本人工值 |
| `record_suggestions` | 受限worker且具备suggest；核对D00任务/结果、素材revision、源文件哈希、字段分工与幂等ID；只写建议/证据 |
| `archive` | owner/admin带revision软归档；不删除任何原文件或对象 |
| `open_content` | 按授权asset选择登记后端，先完整校验文件哈希/大小，再返回只读流；失败不回退原件或其他后端 |

显示名只用于展示，不产生医学证据。`revise`拒绝脚本拥有字段、单/多选字段、医学逐条确认字段及未知字段；来源许可/标签状态也不能由该文本接口变成已确认。D06/D13须补齐对应类型、词表和医学证据验证后，才能扩展人工采用入口。

建议入口拒绝AI推断人工拥有字段，AI也不能代写脚本字段；医学和选择字段的observed建议在本阶段拒绝，等待D06/D13验证，unknown/no_match等状态按D00保留。同一task/attempt/epoch的任务与结果必须完全一致才幂等复用；不同执行应使用新的suggestion_id，不覆盖旧建议。单个批次的建议与完整执行证据一起提交，冲突整体回滚。当前租约存活、取消状态、任务授权有效期和阶段发布仍由D03负责；D01的记录函数不等于一次完整Worker结果发布。没有实际模型推理时不能用这些合成测试宣称模型成功。

授权在每次操作/打开文件时检查。HTTP层须按请求重新调用入口，不直接返回内部路径、数据库连接或Python文件句柄；已打开流的生命周期和撤权处理由后续传输层约束。

## 调用示例与验证

以下为已实现Python接口的参数示例，不是已部署账号/目录。根目录及数据库父目录应先由部署任务创建并维护README；本轮没有建立生产数据库或导入原始素材。

```python
from pathlib import Path
from media_tag.catalog import Catalog, Database, Principal, SourceInput
from media_tag.contracts.templates import load_template_bundle
from media_tag.storage.filesystem import FilesystemBackend

contract = load_template_bundle(
    Path("asset-index-template"),
    expected_fingerprint="40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af",
)
# original_root、object_root、db_path和authenticated_actor来自受信服务配置/认证。
originals = FilesystemBackend(original_root)
objects = FilesystemBackend(object_root, writable=True)
catalog = Catalog(Database(db_path), objects, contract=contract)
asset = catalog.register_file(
    originals, "example.bin", source_backend_id="configured-source",
    principal=authenticated_actor, request_id="example-request", name="Example",
    source=SourceInput(),  # 未提供的来源/署名留空，许可未核实。
)
```

运行工程测试：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/catalog tests/storage -q
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m pip check
```

本轮44项D01测试、加D00共116项通过，普通wheel安装环境同样通过。合成字节和数据库全部在临时目录；覆盖同名异内容、同内容多来源/多账号、请求/别名冲突、迁移与写入失败回滚、并发登记与修订、一致快照、真实Windows junction、实际句柄重定向拒绝、损坏/丢失不回退、原件字节不变、人工值与建议隔离、归档不删文件、物理根目录切换不改身份及OSS明确失败。另以真实模板绑定一次临时合成素材，完成登记、查询、校验读取；素材字节及六个原始契约摘要未变。完整服务端鉴权、视频、模型、导出和备份业务验收仍未执行。
