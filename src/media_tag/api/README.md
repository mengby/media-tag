# Router HTTP API

`app.create_app` binds trusted server identities to hashed bearer credentials and exposes
job/worker scheduling operations and D09 upload/variant planning routes when bound to their services. `config.load_application` validates local deployment
configuration. Versioned `/v1/assets` routes expose D17 read-only file access; semantic search is pending. Neither module starts a service on import. See [operations](../../../docs/router-queue.md).
