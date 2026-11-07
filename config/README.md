# 本地 Router 配置

`*.local.*` 已被忽略。实际根目录、数据库位置及 bearer 凭证摘要只放本地文件；
不从相邻项目读取 `.env`。使用独立随机 token，将其 SHA256（十六进制）映射到受信
`actor_id`/`role`，客户端保管原 token。Worker 使用单独身份，不能共享管理员凭证。

完整字段及已实现启动命令见 [D03 运行说明](../docs/router-queue.md)；D09新增可选 `upload_options`，限额见[上传说明](../docs/upload-variants.md)。D17可将凭证绑定为 `reader`，仍需逐对象read授权；文件契约见[说明](../docs/file-access.md)。D04可选`models`字段与固定权重/CPU-CUDA配置见[模型说明](../docs/model-workers.md)，默认关闭；启用时使用本机绝对模型目录。没有默认账号、
通用 token 或自动生成的生产数据库。新环境须提供六份原始模板并核对其正式摘要。
