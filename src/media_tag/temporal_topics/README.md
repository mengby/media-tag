# Continuous visual topic drafts

CT-13 bounded continuous-video/animation draft core and opt-in Router worker.
Deployment is off by default. No API/config registration, catalogue effective-value
or approval writes are performed by this package.

High-depth, HDR/wide-gamut and unrecognized video pixel formats wait for an
explicit conversion policy; decoding cannot silently turn medical precision into
8-bit model pixels.

`runtime.py` supplies the GPU-one stage/handler; `runner.py` performs full decoding
and inference in a start-gated, offline child owned by a ProcessTree and parent
watch. `process.py` bounds total time, cancellation, logs and response bytes.
`projection.py` rechecks current released producers, bounded artifacts, actual raw
answers, and a second full decode in a bounded CPU-only child. Its caller must
authorize the asset/file first and provide trusted tools (explicitly or through
router.temporal_topic_tools); no cross-account API is supplied here.
`clocks.py` maps selected visual PTS to the file's common review clock without
guessing missing timing. Standalone service callers still own their execution
boundary; optional artifact_root preserves real Router-relative file references.
