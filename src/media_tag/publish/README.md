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

失败保留为 `pending`，记录 `attempts`、`retry_at` 和 `error`，退避从 5 秒到最多 5 分钟。Worker 恢复后继续处理，重试复用原件与已上传对象。运行库同级的 `item-publications/` 暂存清单和证据，成功后清理。原件从现有本地对象库读取。未配置 `item_publication` 时，Worker 继续只做本地处理。当前实现待整链路联调。
