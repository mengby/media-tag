# Similarity candidates

Deterministic visual descriptors and real decoded-audio spectral descriptors.
No model download, semantic classification, automatic duplicate confirmation,
asset merge, label copying or permission transfer is performed. Engines consume
explicit verified bytes/files. The authorized service checks current FileService
gates and revisions for both sides; its ledger is separate from the catalogue.
NumPy is used only by the audio engine and is an existing `vision` optional extra.

Visual/audio recipes are v2. Animation timing is read after the current frame
loads and keeps fractional milliseconds. Selected audio PTS are normalized;
decoding reaches EOF or fails its byte/time budget, never accepts an output-time
cut prefix as complete. Actual PCM sample count and source origin are retained.
Legacy v1 descriptors/candidates must be recomputed before current qualification.
