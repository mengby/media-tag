# All-media contract tests

Structural boundary tests for CT-02: complete topic coverage, unresolved routing,
actual-source/evidence binding, video versus audio modality, and prevention of
fabricated approval states. These use synthetic evidence references and perform
no model calls or real media/medical acceptance.

Producer/source/version/evidence linkage and conservative first-frame routing
are also covered. No local precision exemption is treated as calibrated.
`tests/search/test_topic_analysis.py` exercises actual Router/report verification
with synthetic VLM protocol responses, including tampering and lease release.
