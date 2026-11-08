# 本地 Router 配置

`*.local.*` 已被忽略。实际根目录、数据库位置及 bearer 凭证摘要只放本地文件；
不从相邻项目读取 `.env`。使用独立随机 token，将其 SHA256（十六进制）映射到受信
`actor_id`/`role`，客户端保管原 token。Worker 使用单独身份，不能共享管理员凭证。

完整字段及已实现启动命令见 [D03 运行说明](../docs/router-queue.md)；D09新增可选 `upload_options`，限额见[上传说明](../docs/upload-variants.md)。D17可将凭证绑定为 `reader`，仍需逐对象read授权；文件契约见[说明](../docs/file-access.md)。D04可选`models`字段与固定权重/CPU-CUDA配置见[模型说明](../docs/model-workers.md)，默认关闭；启用时使用本机绝对模型目录。D10可独立设置`redaction.enabled=true`，复核人必须显式绑定`redaction.reviewers`及对象read授权，默认空列表不允许批准；见[打码说明](../docs/face-redaction.md)。D11通过`retime.enabled=true`独立启用CPU变速；`retime.reviewers`用于隐私输出的新哈希复核，默认空列表。见[变速说明](../docs/fixed-retime.md)。D12通过`web_enabled=true`启用`/ui/`人员页，同源会话与入库边界见[人员页说明](../docs/personnel-ui.md)，默认关闭，不需要Node生产服务。D06通过`review.enabled=true`启用字段复核/CSV，医学与整行确认另需`review.reviewers`显式绑定；`review.people`是实际外部人员ID映射，未配置则人员列留空。词表消费需固定manifest/version/hash及trusted_publishers，见[复核与CSV](../docs/review-csv.md)。没有默认账号、
通用 token 或自动生成的生产数据库。新环境须提供六份原始模板并核对其正式摘要。

D13新增可选 `terminology`：默认关闭，显式 `managers` / `publishers` 分别控制词条审核和发布/回滚，admin不自动获得这些能力。分类/别名快照独立版本且绑定发布ID；D06可消费D13分类快照但仍需明确pin并重载，见 [D13说明](../docs/terminology-release.md)。无正式医学词表或默认管理账号。

D14新增可选 `text`，须启用terminology；默认关闭模型。CPU BGE的绝对模型目录、拒识配置、解析和文本索引整组激活见 [D14说明](../docs/terminology-worker.md)。没有正式词表时明确降级，启用功能不下载模型，不产生假医学标签。

D15新增 `search.enabled=true`（须启用text），默认5000个授权版本预算；检索/反馈/草稿管理页面、权限和有限别名CSV见 [D15说明](../docs/search-feedback.md)。正式管理/发布/医学审核能力仍分别配置，不因启用搜索而授予。

D18独立桥接配置见 `library-client.example.json`（拟定填写示例，尚未指向实际服务），复制到忽略的 `*.local.json` 并填写 HTTPS/loopback origin 与绝对 reader-token 文件路径。正文凭证不写进JSON、工具参数或宿主启动参数；不复用相邻`.env`。stdio入口和依赖见 [MCP说明](../docs/mcp-adapter.md)，本机Codex宿主与实际配置现已验证，见后续说明。

## Codex本机入口

`codex-mcp.example.toml`与本机已安装入口等价，指向项目`.venv`及私有reader-client配置；不是凭证文件，不应覆盖其他用户设置。实际宿主与桌面重载边界见 [说明](../docs/codex-host.md)。
