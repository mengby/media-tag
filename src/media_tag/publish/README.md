# 本地发布器

从显式指定的目录库创建只读一致快照，复用现有文件授权/隐私/证据验证，导出选定素材及祖先、可移植字段/证据投影。不会启动Router、恢复队列或加载模型。原报告摘要保留，含本机路径的原报告不上传；AI初稿始终有证据且不写成人工批准。

命令：`media-tag publish build --input <私有导出配置> --destination <新目录>`；`media-tag publish push --bundle <目录> --config <私有发布客户端配置>`。安装publish可选依赖及固定契约wheel后使用。上传经HTTPS或本机loopback，拒绝重定向；本轮不含真实OSS接入。
