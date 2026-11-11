# Local processing queue adapter

Explicit catalogue-asset requests for cleaning copies/video clips and image masks.
Reuse the existing Router and LocalExecutor; no new execution queue or database
migration. Server-side source/CAS/edit/file gates, verified private input snapshots
and contained CPU subprocesses precede artifact adoption. Outputs remain private
pending registration/review; no original registration or approval is performed.
HTTP installation is in `http.py`; `LocalConfig.processing` and the configured
executor dispatch are implemented in `api/config.py`, disabled by default.
