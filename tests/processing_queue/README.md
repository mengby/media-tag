# Processing queue tests

Isolated synthetic catalogues, private workers and generated media only. No production
files, templates, configuration, network or model calls are used.

`test_http_config.py` boots actual isolated LocalConfig/API/executor instances.
Only the original template loader is replaced with the isolated synthetic contract;
source storage, catalogue, authentication, native processing and queue are real.

Derived tests exercise migration 12, actual registration/full decode, independent
privacy/quality/watermark review, authorized private comparison, stable file gates,
source/task races, restart/backoff fairness and isolated backup/restore. Reviewer
names and approvals in tests are explicit synthetic protocol actors only.
