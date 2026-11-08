# D06 人工字段复核与 CSV 快照

2026-11-08：D06核心、HTTP接口及人员管理页已实现，使用项目`.venv`，没有新增第三方依赖或下载模型。关联R05/R11、N02/N03/N05/N06，覆盖A06/A09的工程部分；不代替代表性医学素材评测、来源许可核实或指定人员业务签收。

## 启用与实际入口

已有本地配置增加`"review": {"enabled": true, "reviewers": [], "people": {}}`，并设置`web_enabled=true`。启动命令仍为：

```powershell
& .\.venv\Scripts\python.exe -m media_tag serve --config config/YOUR.local.json --port 8000
# http://127.0.0.1:8000/ui/ → 标签复核与导出
& .\.venv\Scripts\python.exe -m media_tag work-once --config config/YOUR.local.json --worker-id YOUR_WORKER_ID
```

`review.enabled`默认false，不擅自启用生产人员或服务。非医学字段由有对象edit权限的editor/admin操作；reader/worker不能复核或导出。医学字段和整行索引确认另需显式`review.reviewers`人员绑定及对象edit权限，默认空列表不能批准。它与`redaction.reviewers`/`retime.reviewers`相互独立，不因拥有某项身份而自动获得另一项权限。

管理页展示最近200个有权编辑素材及200个本人缺口；可直接通过固定ID调用接口，当前未实现跨库语义检索。先选原图/连续片段，再记录观察依据，明确选择依据后**每次只保存一个字段**；不提供批量医学确认。建议、当前有效值、拒绝记录、源版本与错误分别显示。原生播放器可定位并播放观察/建议区间；时间填写当前文件共同起点归一化秒，Chromium定位时加回登记的源origin。已实测非零起点5秒的源视频，0.25～0.75秒映射为5.25～5.75秒，未再次变速。连续证据要求已知共同源时钟，不能猜缺失的时间基准。

## 观察、建议和字段决定

- `review_observations`保存真实鉴权actor、源SHA256、实际查看声明、说明及可选连续区间/来源页面，不能跨文件使用。来源登记记录只作可核对草稿，不自动确认来源或许可。观察声明是操作者的明确记录，不表示系统代替人观看或认可医学结论。
- 接受建议绑定run_id、suggestion_id、实际源哈希、当前生产任务/租约及原始证据哈希；已取消、重规划、缺失或损坏证据不能采用。依据图像通过私有入口读取，失败不返回原件或其他帧替代。界面可定位片段，也保留模型/规则版本。
- `review_decisions`记录单字段manual/accept/reject、说明、观察引用、政策摘要和实际metadata revision。有效值由服务器生成actor、时间和revision，客户端不能指定虚假人员。拒绝建议不清空既有有效值。未观察、无匹配、不适用、执行失败各有独立状态与原因。
- 非空医学字段需指定复核人的实际媒体观察；变化方向需要连续视频/动图片段，静帧、文件名和来源文字不能证明完整变化。普通视频“无”人脸需完整连续观察；“已处理副本”必须绑定实际已批准的打码输出，不允许模型或普通字段操作冒充隐私完成。
- 脚本字段、素材ID、标签状态和打标人不能从普通字段接口改写。URL拒绝临时签名和凭证参数；来源/许可/署名需可追溯人工记录，非“未核实”的许可声明还需原始页面引用，服务不会自动判断授权成立。

字段类型按原始说明表校验：标量/多选、闭合枚举、非负有限数字、日期及URL。正式闭合选项从实际模板读取，尾部“适用对象”注释不作为枚举；含省略项/锁定列表的字段不从示例猜完整词表。缺失发布时医学选项保持空白，可记录no_match及候选说明；搜索或点击不能发布词条。

D01仍只接受原有非医学文本修改；D06启用后仅为受信结果采用增加类型/选项/证据验证能力。观察与AI建议不会自行变成有效值，AI重跑仍不覆盖人工修订。

## 整行索引确认

`index_confirmations`是独立、不可变的确认/撤回历史。确认要求全部可复核字段已经逐项形成D06有效决定，且状态为observed或经明确说明的not_applicable；unknown/no_match/failed不能被批量算作已确认。确认还检查本人完整源观察、当前源哈希、全部字段类型、对应决定与当前词表政策版本，并用metadata revision作CAS。

旧`assets.index_status`的ai_draft约束不改；读取时根据确认记录及当前源/名称/有效值/政策摘要投影ai_draft、confirmed、needs_review。修改有效值、名称或切换词表政策使旧确认失效；单独ACL修订或新AI建议不伪装标签变化。历史决定仍保留。

CSV只有当前索引确认有效时使用原枚举“已人工确认”，否则为“AI初稿”。这不批准人脸输出、不改变来源许可、共享范围或当前剪辑用途，也不代替最终医学审核。

## 缺口表

缺口是明确提出的画面请求，不是对已存在素材的观察。使用实际稳定G-ID，保存提出者鉴权身份、首提时间、次数、修订和事件。相同owner及规范化请求内容复用同一缺口；相同request_id重试不累计，新请求再次提出才加1。改变身份字段时更新去重指纹，冲突要求显式处理，不能悄悄合并两个缺口。

处理状态、关联素材由人明确修订；“已补”需至少一个有读取权限且未被隐私/生产门槛阻止的素材。重复提出只累计次数和事件，不自动改写已有的人工处理状态。原话、任务和已搜索地点仅按提供者明确输入保存，不从文件名或查询失败猜测。AI自动提出缺口及术语候选发布仍待后续任务。

## 35/14列导出与原子发布

使用本地六份原始契约的实际表头/列序，UTF-8 BOM、标准CSV引号/CRLF；不增加内部字段，不复制原始模板进Git。导出必须明确选择最多500个素材和500个缺口及其revision/源哈希，不把全库默认打包。两张表可包含空表头，仍完整写出35/14列。

有效值优先；默认不导出AI候选。显式勾选AI初稿时，只采用有当前证据且未被拒绝的非医学建议，并在报告列明待确认的run/suggestion；人工unknown等空状态也阻止AI回填。医学建议不自动写到正式医学列。

技术值来自已验证源版本的探测：方向/尺寸、时长1位小数、实际透明像素及音频可听性。图片没有时长，音频没有方向/分辨率；观察到透明/可听正例可写true，否定须有完整检查依据；未能确定的透明/静音结果保留空白，不能把unknown写成false。许可默认未核实。正式素材ID使用登记的稳定asset_id，旧编号继续由D01别名保存；没有已选定别名映射时不猜某一别名重写身份。

“本地路径”不暴露盘符、对象后端位置或来源暂存路径，缺映射时为空；下载链接使用固定版本的相对授权入口，质量报告标明需服务域名上下文，不持久化临时签名URL。附件列不伪造飞书attachment token。`review.people`仅供管理员明确映射内部actor到实际外部人员ID；未配置时“打标人”“提出人”留空并在报告记录实际actor及缺少映射，不能填假员工或把AI文字当人员对象。关联值是稳定素材ID，不冒充飞书record_id。飞书对象写入仍属D08。

文本公式防护覆盖前导空白、控制字符/零宽字符后的`= + - @`及制表/换行，CSV表示添加单引号；库内原文保持不变。质量报告逐条记录转义、未知/不适用/失败、未确认医学字段、缺失路径/附件/人员映射和AI初稿；技术导出成功不代表字段内容已准确或许可已核实。

从同一SQLite读事务捕获两表及授权引用。每次导出在`jobs/export-…/attempt-N/`记录输入、README、两张CSV、quality.json及manifest.json。写临时文件、刷新/核对哈希、原子替换本次新文件，复验整个bundle后，再以单一事务确认快照未变化并更新本人当前导出指针。失败保留上一份成功快照的字节与指针；失败重试创建新attempt，300秒过期租约采用fencing拒绝旧发布。

只有DB-ready文件可下载；相同有效快照按哈希复用，缓存损坏时不伪装成功，新请求可以重新生成。每次下载重新检查本人导出范围、相关素材/缺口revision、当前权限、隐私来源链及文件哈希；撤权/撤回复核后旧入口失效。历史字节不被改写，当前版本变化后需重新导出，不能把旧快照当当前已确认索引。

## 已实现接口

| 路径 | 内容 |
| --- | --- |
| `GET /review/queue`、`GET /review/assets/{asset_id}` | 对象edit范围、当前值/建议/来源/证据及选项状态 |
| `POST /review/assets/{asset_id}/observations` | Observation：request_id/expected_revision/source_sha256/kind/note/viewed/区间/可选reference |
| `POST /review/assets/{asset_id}/fields` | 单FieldDecision：固定源/revision、field、manual/accept/reject、显式value或run/suggestion、观察ID、note、dynamic |
| `POST /review/assets/{asset_id}/confirmation` | 独立confirm/revoke，固定源/revision及完整观察声明 |
| `GET /review/assets/{asset_id}/evidence/{run_id}/{evidence_id}?sha256=…` | 经当前任务与哈希核对的私有建议证据 |
| `GET/POST /review/gaps`、`POST /review/gaps/{gap_id}` | 私有缺口列表、新增/累计、CAS修订 |
| `POST /review/exports` | 明确assets/gaps选择、request_id及可选include_ai_drafts，返回完整快照 |
| `GET /review/exports/latest`、`GET /review/exports/{export_id}` | 本人的当前成功导出或实际阶段状态 |
| `GET /review/exports/{export_id}/content/{assets.csv或gaps.csv或quality.json或manifest.json}` | 固定快照、实时权限/版本/哈希检查 |

接口使用既有Bearer及D12同源HttpOnly会话，无静态媒体/CSV目录、任意路径或客户端角色参数。未实现名为review/export的CLI子命令，不把拟定命令当作可运行入口。

## 词表消费边界与实际验证

目前没有正式医学分类发布，不通过D06创建发布。可选`taxonomy_manifest`必须为本地绝对路径，并显式绑定taxonomy_version、manifest SHA256和trusted_publishers；使用既有published manifest入口复验内容哈希。消费适配子集为`d06-taxonomy-options-v1`，含template_fingerprint与fields的外部锁定选项数组，严格绑定本项目契约，不能把模板示例当正式taxonomy。D13仍需负责完整词条/关系语义、发布权限、审核和回滚；D06消费测试使用明确隔离的合成发布，不表示已有生产词表。

新增38项Python工程测试，涵盖字段权限/证据/CAS/状态、精确建议采用/拒绝、重跑保护、词表门槛、单字段确认、整行失效、缺口计数/关联、35/14列、公式/未知值、失败恢复、损坏缓存及隐私撤回。项目`.venv`与普通wheel全量438项通过；无视觉模型依赖的27包基础环境369项核心回归通过，另有4项Node测试及真实浏览器闭环。验证命令与完整结果见[进度](PROGRESS.md)。

真实六模板浏览器批次已完成合成原图及非零起点视频上传、连续区间定位、两个文本字段与一个片段字段决定、缺口两次累计及两表下载；原文/源哈希不变，未生成医学分类有效值、整行确认、隐私批准或许可核实。桌面与390px布局已检查。业务A06独立集、D13正式词表与真实审核人员、D15语义管理、D07常驻部署及恢复仍待后续。
