# Router HTTP API

`app.create_app` binds trusted server identities to hashed bearer credentials and exposes
job/worker scheduling operations. `config.load_application` validates local deployment
configuration. Neither module starts a service on import. See [operations](../../../docs/router-queue.md).
