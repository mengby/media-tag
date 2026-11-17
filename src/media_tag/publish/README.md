# 本地发布器

从显式指定的目录库创建只读一致快照，复用现有文件授权/隐私/证据验证，导出选定素材及祖先、可移植字段/证据投影。不会启动Router、恢复队列或加载模型。原报告摘要保留，含本机路径的原报告不上传；AI初稿始终有证据且不写成人工批准。

命令：`media-tag publish build --input <私有导出配置> --destination <新目录>`；`media-tag publish push --bundle <目录> --config <私有发布客户端配置>`。安装publish可选依赖及固定契约wheel后使用。上传经HTTPS或本机loopback，拒绝重定向；本轮不含真实OSS接入。

## 逐条发布

本机 Worker 支持在后台逐条发布。启用方式是在私有运行配置中增加下列字段，安装已固定版本的契约 wheel 和本仓 `publish` 可选依赖。将示例值换成本机已配置的素材所有者、可达的云服务、发布专用令牌文件以及本机发布代码的 40 位 Git 提交号。

```json
{
  "item_publication": {
    "owner_id": "<本地素材所有者>",
    "base_url": "https://<media-cloud域名>",
    "token_file": "D:/private/media-publisher.token",
    "producer_revision": "<40位提交号>"
  }
}
```

纳入打标前清洗的原件完成打标后，Worker 在本地数据库的 `item_deliveries` 记录版本。后台发布线程复用原清洗报告、十字段初稿及证据，按 SHA256 向云端查缺，本机直接 PUT 缺少的对象到 OSS。随后把标签 JSON 提交至云端 `index-item` 接口。单条收到匹配的回执即记为 `indexed`。候选和待审状态随版本保留。

失败保留为 `pending`，记录 `attempts`、`retry_at` 和 `error`，退避从 5 秒到最多 5 分钟。Worker 恢复后继续处理，重试复用原件与已上传对象。运行库同级的 `item-publications/` 暂存清单和证据，成功后清理。

使用 WY 内网索引服务时，`ssh_host` 指定本机已信任的 SSH 主机别名，`base_url` 指向该主机上的回环 HTTP 服务，`token_file` 是远端发布凭证的绝对路径。索引请求在该主机上携带凭证；素材仍通过已验证的 OSS 签名上传。部署时根据 WY 实际账号映射设置 `published_owner_id`，仅适用于已验证归属的单个原件。可配置 `required_precision_provider`，仅在指定提供方完成该素材的正式打标后入发布队列。

医学视频批次的本机入口持有批次独占锁，并调用 `video_batch.follow`。一个线程按成功标签登记原件，发布线程从已登记且有成功标签的视频中取材。每个源 SHA256 同时最多派发一次，重启时从匹配的 indexed 回执恢复。单项继续使用批次自己的权限、就绪检查和完整打包流程。

并发试验由批次入口传入阶段名、发布并发和派发时长。每阶段等待在途任务结束后才切换。`wy-windows.jsonl` 记录完整窗口起止时间，`wy-stage-timings.jsonl` 记录发布尝试及失败阶段。吞吐按窗口内去重成功回执除以含收尾时间的实际分钟数计算。打包、云端准备、申请上传地址、OSS 上传和索引确认分别计时，已存在的对象可以跳过传输。
