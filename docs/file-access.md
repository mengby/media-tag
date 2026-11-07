# D17 授权文件、预览与下载基础接口

日期：2026-11-07。状态：本次文件基础范围已实现，D17 整项仍实施中。新增 38 项专项测试，全量 277 项；使用项目 `.venv`，沿用 27 包哈希锁，无新依赖或数据库迁移。对应 R12/R13、N01/N02/N03，覆盖 I03/I04/I05 的本机工程前置；不代表分页语义检索、MCP、跨机器/剪辑宿主或 D17/I 系列整体验收完成。

## 已实现的只读契约

所有接口沿用服务端配置的 bearer 身份，错误/清单版本为 `d17-files-v1`。新增 `Principal.role=reader`，允许按对象权限读文件，不允许写目录、提交上传/计划、领取任务、修改/取消任务或发布。不能在请求体或 URL 中指定身份、角色、后端路径或下载地址。现有 editor/admin 同样经过文件权限与隐私门槛，Worker 不能通过这些接口取文件。

| 方法与路径 | 内容 |
| --- | --- |
| `GET /v1/assets/{asset_id}` | 指定不可变文件版本的详情、当前 metadata revision、标签建议/有效值、技术信息、来源及可见同组版本 |
| `GET /v1/assets/{asset_id}/download-manifest?sha256=…` | 核验实际对象，返回固定哈希、字节数、ETag、媒体类型提示、技术/变换记录和稳定下载入口 |
| `GET /v1/assets/{asset_id}/preview-manifest?sha256=…` | 返回已授权的原生媒体读取和实际 D02 PNG 缩略图；未生成/等待/失败/取消/不适用分别表示 |
| `GET / HEAD /v1/assets/{asset_id}/content?sha256=…` | 固定完整文件，attachment；GET 支持单个 bytes Range |
| `GET / HEAD /v1/assets/{asset_id}/preview-content?sha256=…` | 同一固定版本的原生 inline 媒体，支持 Range；未知二进制拒绝 inline |
| `GET / HEAD /v1/assets/{asset_id}/previews/{preview_id}/content?sha256=…&preview_sha256=…` | 同时绑定源/缩略图哈希的受保护 PNG，资源 ID 为服务端生成的不可解析标识 |

清单只返回相对 `/v1/…` 入口，不取 Host 头生成绝对地址，不返回 Windows/SMB/OSS 路径、对象 key、临时签名链接或凭证。调用者相对已配置的可信服务 origin 解析地址，逐次携带自己的 Authorization。原始出处页面只是数据，不自动抓取。

详情与同组列表逐对象过滤；同组列表最多 50 个可见版本，`versions_truncated` 标记截断，完整检索/分页仍待后续 D17。D09 旧 `/assets/{id}/variants` 继续作为关系接口保留；文件能力以本次 `/v1` 清单为准。获取一个衍生版本不授权读取原件或其他版本。

## 固定版本、存储与撤回

文件/清单入口强制提供预期 SHA256；asset_id 对应文件不可变，metadata revision 仍可增加。错哈希返回 412，不静默改为当前另一个版本。下载清单打开实际配置后端并核验完整 SHA256/大小；缺失、损坏或后端未配置返回 503，不尝试历史来源路径或其他版本。改变受控后端映射不改变 asset_id、URL 或哈希，OSS 真接入留 D08，不在本轮生成重定向/签名。

详情可展示未知技术字段；可选探测报告缺失/损坏时 `technical.report_state=unavailable`，不假造值，也不阻塞已校验字节下载。媒体类型来自文件签名提示，`media_type_verification=signature`，不是全面格式/解码保证；具体技术数据沿用已接受的 D02 报告或 D09 校验记录。调用方仍需校验完整下载哈希/实际格式。未知内容使用 octet-stream/.bin，不以内联 HTML/SVG 方式执行。

每次请求先认证、对象授权、归档/隐私/源哈希检查，再处理 Range/ETag；304 和 HEAD 也不能绕过鉴权或文件校验。完整后端校验后、发送响应前再次检查权限，流式每块继续复查归档/授权/隐私状态，检测当前本地句柄的大小/mtime 变化。传输中撤权或对象变化会中止该响应；已发出的字节无法收回，旧缓存每次复用仍须重新鉴权。客户端断开、HEAD、304、错误和正常结束均关闭已验证句柄。

目前每个内容/Range 请求都完整验证底层对象，成本随文件大小增长；不把路径/stat 缓存当作完整性证明。容量、吞吐、长连接限流与部署调优留 D07；本轮只做本机功能/故障验证。

## 隐私与衍生版本

无打码且登记验证完整的衍生输出可以按独立权限访问。检查整条 lineage：任何祖先仍为 privacy pending、来源失效或生产任务取消/旧 generation/未释放，文件入口均拒绝。不能先生成待复核打码版、再派生一个无 mask 版本来绕开门槛。

当前数据库只有 pending/not_applicable，没有已复核状态；本轮没有隐私批准操作。待复核打码版本通过公共文件入口对 owner/admin 也拒绝，D10 的专用人工复核预览/批准仍待实现。原件仍按其自身显式对象权限访问，获取失败的打码版本绝不回退原件。新导入原件默认私有，不从画面推断隐私许可。

清单记录现有 recipe_hash、累计倍速和 parent→output time_map，并明确 `apply_transform_again=false`；字节已经是选定文件版本，下载不会再变速、裁剪或打码。原生预览为当前文件的 identity 映射，时间基准来自实际选中流的起点/时长，未知保留 null，不猜零点。标签仍是 AI 初稿/人工有效值原状态，许可仍未核实，`current_use_review=not_checked`，不代替剪辑用途或医学审核。

## 预览行为

GET 预览不会新建任务或启动模型。上传原有自动分析可产生 D02 evidence；只有已采用、当前、成功、未取消且资源已释放的结果能提供缩略图。每张 PNG 重新校验文件哈希、类型、尺寸/字节预算；不透传任意 jobs 路径。缩略图绑定所选 asset/hash，保存实际源时间与登记的任务预处理版本（declared_preprocess_version，不作为额外模型调用证明），最多 12 张、每张 8MiB/2048² 像素，稀疏帧不代表连续观察。

`thumbnail_state` 区分 ready、not_requested、pending、failed、cancelled、not_applicable；返回 status_query 供重查。音频没有视觉帧不算失败。原生媒体可读时主 state 仍可 ready，缩略图阶段状态独立保留。源文件或 PNG 实际损坏不会当成“无匹配”，缩略图失败不返回原件缩略图冒充目标输出。

原生视频/音频/图片直接流式读取所选版本，清单明确 whole_file、identity 映射和 compatibility=client_dependent。没有新转码代理、HLS、缩略图生成器或自动切片；宿主不支持该容器时应报告限制。没有把任意图片/文字当作已观看连续视频。

## HTTP 条件与错误

文件 ETag 为完整表示的强 SHA256 标签。支持 If-Match（强比较）、If-None-Match（弱比较，匹配返回 304）、If-Range（仅强 ETag）；If-Range 不匹配/日期格式时忽略 Range 返回同一固定文件的 200 全量响应，客户端须丢弃不匹配的旧片段。HEAD 忽略 Range，返回全量 Content-Length，无 body。不提供 Last-Modified/日期条件实现。

支持 `bytes=a-b`、`bytes=a-`、`bytes=-n`，有效单区间返回 206、Content-Range 和准确长度；超尾端点截断。无效/不可满足字节范围返回 416 和 `bytes */size`。多区间和其他单位忽略后返回单次全量 200，不做 multipart 放大。响应使用 private/no-store、Vary: Authorization、nosniff、no-referrer；安全 ASCII fallback＋UTF-8 filename*，剥离路径/控制字符并按实际类型使用安全扩展名。

v1 错误形如 `{schema_version, error:{code,retryable}}`：401 缺凭证，403 对象/角色无权，404 版本/预览不可用，409 隐私/lineage/生产状态阻止，412 哈希或 ETag 条件失败，415 不支持 inline，416 范围错误，422 参数错误，503 文件/后端/数据库不可用。错误不回显原始输入、后端异常、令牌或签名链接。流已开始后的撤权不能改写为 JSON 状态，只能中止传输。

HTTP 行为依据：[RFC 9110 条件请求与 Range](https://www.rfc-editor.org/rfc/rfc9110.html)、[RFC 6266 下载文件名](https://www.rfc-editor.org/rfc/rfc6266.html)。本实现是上述明确子集，不宣称实现所有 HTTP 条件或多区间。

## 验证与后续

新增 38 项专项测试：只读身份/写拒绝、逐版本权限、旧清单撤回、Range/HEAD/ETag、缺失/损坏/未配置后端、实际 PNG/音视频、原生 PTS、预览失败/取消/不适用、待复核及其下游绕过、传输句柄关闭、Unicode/注入文件名、相对入口与后端映射稳定性。合成衍生生产阶段只验证协议，不冒充 D10/D11 渲染器。项目环境及普通 wheel 安装环境各跑全量 277 项，pip check 通过。

真实六模板批次：`jobs/d17-files-validation-20261107-e6992419/validation.json`。实际 loopback 上传/重启续传→自动 probe/evidence→reader 清单→两次 Range 下载/拼接/原子落盘→完整 SHA256/FFmpeg 解码；3 张 PNG 哈希、HEAD、304、只读拒写、撤权后拒绝旧清单均通过。原件未变，metadata revision 仅因明确的测试授权/撤权从 1→3。媒体/数据库/报告/凭证摘要配置忽略 Git，原 token 只在验证进程内存。

D17 整项仍待 D15 语义检索/分页和完整 I 验收；D18/MCP、D19 剪辑适配、D20 跨机器/宿主联调未做。TLS/受限账户/常驻服务和容量恢复留 D07，隐私输出复核留 D10。本轮不修改相邻项目的取材、预览或导出规则。
