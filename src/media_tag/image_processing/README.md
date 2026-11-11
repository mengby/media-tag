# Explicit image and animated-sticker processing

Deterministic region masking on caller-authorized, read-only local sources.
PNG/APNG outputs preserve composed RGBA frames, exact APNG frame delays and
animation play counts. No face detection, generative restoration, automatic
licensing decision or human approval is performed. The caller must keep these
private outputs behind the existing hash-bound privacy/quality review gates.

`inspect()` performs the same bounded read-only canonical decode without creating
output artifacts, exposing source RGBA frame hashes. Explicit unmasked-frame
declarations bind to those hashes and record a masking request, not face detection
or approval; every declared frame remains subject to full output review.
