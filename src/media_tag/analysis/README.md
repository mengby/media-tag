# All-media analysis

CT-02/CT-04 foundations for the cleaning/tagging implementation. `contracts.py` defines
ten topic-field assessments, source-bound visual/audio evidence, explicit pending
actions, unapproved cleaning defaults and provider capabilities. It imports no
model and performs no network/file operations by itself.

`schema.py` adds migration 11 for requester-scoped durable analysis intents.
`intake.py` records them in the same transaction as a new original, completed
upload or trusted derived result. It does not merge owners, grant edit rights,
publish to the library, execute models or claim field completion. `service.py`
and `http.py` expose authenticated read-only state. With `deployment.py` explicitly
enabled (off by default), the configured worker dispatches known media types using
trusted credential principals and Router's durable request bindings. Processing's
fast job stays independent; compatible library jobs coalesce through Router.
Audio gets only a probe until actual audio providers are added. Unknown media types
first get a real probe; source/report verification precedes type adoption and adding
missing stages to that job, preserving the probe even after replan ACK loss.
Cancelled/failed bindings are not silently
replaced. Technical success never claims ten-field or human-review completion.

Existing databases are not backfilled on read. Old v9/v10 backups remain
readable and migrate to v11 only when explicitly opened by the current catalogue
bootstrap. The deployed instance has not been opened or restarted by this work.

These are structural contracts. Artifact hashes, vocabulary options, permissions,
current leases, reusable producer results and human decisions must still be
verified by their owning services before adoption. Schema validation is neither
actual inference nor a privacy/medical approval.
