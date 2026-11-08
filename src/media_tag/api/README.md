# Router HTTP API

`app.create_app` binds trusted server identities to hashed bearer credentials and exposes
job/worker scheduling operations and D09 upload/variant planning routes when bound to their services. `config.load_application` validates local deployment
configuration. Versioned `/v1/assets` routes expose D17 read-only file access; semantic search is pending. Neither module starts a service on import. See [operations](../../../docs/router-queue.md).

D12由`web_enabled=true`可选安装同源人员页面/会话/入库入口，见`web/`与docs/personnel-ui.md；默认关闭，媒体仍由逐对象文件服务授权，非公共静态目录。
