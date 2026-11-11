# Ten-field independent evaluation

Offline frozen dataset/annotation imports, source/group split isolation, hash-bound
AnalysisDocument bundles and explicit denominators. No model calls, service writes,
automatic calibration activation or business approval.

`freeze_catalog_export` optionally freezes the caller's current authorized catalogue
projection into a private, source/hash-bound offline bundle. It never starts model
work or writes catalogue records; see `docs/topic-evaluation.md` for supported stages,
actor scope, read budgets, original-report preservation and final metadata fences.
