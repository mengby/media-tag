# Temporal draft tests

Synthetic GIF/APNG and short FFmpeg videos in private temporary directories; mocked
generation only. No actual GPU, model weights, network or production data.

Worker tests execute a real isolated Python subprocess with a wire-protocol mock
generator and real GIF/FFmpeg decoding; production synthetic rejection stays on.
They cover actual source/clock/manifest revalidation, raw-answer recomputation,
current attempts, budgets, process-tree termination and common review-clock
mapping when an audio stream starts before the selected video stream.
