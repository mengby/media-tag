from hashlib import sha256
import json
from pathlib import Path
import os

import pytest

from media_tag.catalog import AccessDenied, Principal
from media_tag.storage import IntegrityError, UnsafePath
from media_tag.workers.evidence import EvidenceOptions, evidence_records
from media_tag.workers.probe import MediaError, ProbeOptions


def test_registered_chinese_path_real_frames_and_hashes(media_env, generated_media, media_tools):
    env = media_env
    item = env.register(generated_media.video, "合成中文素材.mp4")
    before = (env.originals / "合成中文素材.mp4").read_bytes()
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-job")
    assert report["status"] == "succeeded"
    frames = report["evidence"]["data"]["frames"]
    assert len(frames) == 3
    source_frames = media_tools.probe_run(generated_media.video, timeout=20, extra=["-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time"])["frames"]
    for frame in frames:
        assert frame["source_pts_seconds"] == pytest.approx(float(source_frames[frame["source_frame_index"]]["best_effort_timestamp_time"]), abs=0.002)
        output = env.workspace / frame["artifact"]["ref"]
        assert sha256(output.read_bytes()).hexdigest() == frame["artifact"]["sha256"]
        assert frame["source_sha256"] == item.sha256
    assert len(evidence_records(report)) == 3
    assert (env.originals / "合成中文素材.mp4").read_bytes() == before
    assert env.catalog.get_asset(item.asset_id, principal=env.actor) == item


@pytest.mark.parametrize("fixture", ["rotated", "vfr", "offset"])
def test_rotated_vfr_and_nonzero_source_time_are_real(media_env, generated_media, media_tools, fixture):
    env = media_env
    source = getattr(generated_media, fixture)
    item = env.register(source)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-time")
    assert report["status"] == "succeeded", report
    info = report["probe"]["data"]
    frames = report["evidence"]["data"]["frames"]
    source_frames = media_tools.probe_run(source, timeout=20, extra=["-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time"])["frames"]
    for frame in frames:
        assert frame["source_pts_seconds"] == pytest.approx(float(source_frames[frame["source_frame_index"]]["best_effort_timestamp_time"]), abs=0.002)
        assert frame["normalized_seconds"] == pytest.approx(frame["source_pts_seconds"] - info["start_seconds"], abs=0.002)
    if fixture == "rotated":
        assert frames[0]["height"] > frames[0]["width"]
    if fixture == "offset":
        assert frames[0]["source_pts_seconds"] >= 5 and frames[0]["normalized_seconds"] == pytest.approx(0)


def test_stage_reuse_changed_evidence_and_corrupt_output_retry(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.video)
    first = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-reuse")
    again = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-reuse")
    assert again["probe"]["cache_hit"] and again["evidence"]["cache_hit"]
    changed = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-reuse", evidence_options=EvidenceOptions(max_frames=1))
    assert changed["probe"]["cache_hit"] and not changed["evidence"]["cache_hit"]
    artifact = first["evidence"]["data"]["frames"][0]["artifact"]
    (env.workspace / artifact["ref"]).unlink()
    repaired = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-reuse", attempt=2)
    assert repaired["probe"]["cache_hit"] and not repaired["evidence"]["cache_hit"]
    assert repaired["status"] == "succeeded"
    assert repaired["evidence"]["cache_rejections"]


def test_packet_horizon_and_frame_budgets_are_explicit(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.video)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-budget",
                                  evidence_options=EvidenceOptions(max_frames=2, max_packets=4, horizon_seconds=0.4))
    data = report["evidence"]["data"]
    assert report["status"] == "succeeded"
    assert len(data["frames"]) <= 2 and data["indexed_frame_count"] <= 4
    assert max(data["sampled_normalized_seconds"]) <= 0.401
    assert data["full_video_observed"] is False and data["continuous_observation"] is False
    assert data["unobserved_between_samples"] is True


def test_still_and_audio_use_different_evidence_paths(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.exif)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-image")
    record = evidence_records(report)[0]
    assert record.kind == "image" and record.start_seconds is None
    frame = report["evidence"]["data"]["frames"][0]
    assert (frame["width"], frame["height"]) == (12, 20)


def test_image_preview_decode_error_is_a_recorded_stage_failure(media_env, generated_media, monkeypatch):
    env = media_env
    item = env.register(generated_media.opaque)
    def failed_preview(image):
        raise OSError("synthetic injected preview decoder failure")
    monkeypatch.setattr("media_tag.workers.evidence.ImageOps.exif_transpose", failed_preview)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-preview-failure")
    assert report["status"] == "partial"
    assert report["evidence"]["errors"][0]["code"] == "image_evidence_failed"


def test_audio_frame_extraction_not_applicable(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.silent)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-audio")
    assert report["status"] == "succeeded" and report["evidence"]["status"] == "not_applicable"
    assert evidence_records(report) == ()


def test_failure_is_recorded_and_requires_new_attempt(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.corrupt)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-damaged")
    assert report["status"] == "failed" and report["evidence"]["status"] == "blocked"
    records = list((env.workspace / "jobs/synthetic-damaged").glob("runs/*/attempt-1/stage.json"))
    assert len(records) == 1 and json.loads(records[0].read_bytes())["errors"]
    with pytest.raises(MediaError) as error:
        env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-damaged")
    assert error.value.code == "attempt_exists"
    retry = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-damaged", attempt=2)
    assert retry["status"] == "failed"


def test_permission_and_input_budget_are_checked_before_cached_result(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.video)
    env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-private")
    with pytest.raises(AccessDenied):
        env.analyzer.analyze(item.asset_id, principal=Principal(actor_id="synthetic-other"), job_id="synthetic-private")
    with pytest.raises(MediaError) as error:
        env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-private", probe_options=ProbeOptions(max_input_bytes=1))
    assert error.value.code == "input_budget"


def test_long_video_never_claims_full_sampling(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.long_video)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-long")
    assert report["probe"]["data"]["duration_seconds"] >= 79
    data = report["evidence"]["data"]
    assert len(data["frames"]) <= 3
    assert max(data["sampled_normalized_seconds"]) <= 30.001
    assert data["full_video_observed"] is False


def test_private_staged_input_corruption_cannot_be_reused(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.video)
    env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-input-check")
    copies = list((env.workspace / "jobs/synthetic-input-check/inputs").glob("objects/*/*"))
    assert len(copies) == 1
    copies[0].write_bytes(b"synthetic corrupted cached input")
    with pytest.raises(IntegrityError):
        env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-input-check")


def test_gif_frames_have_native_timestamps(media_env, generated_media):
    env = media_env
    item = env.register(generated_media.animated)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-gif")
    assert report["status"] == "succeeded", report
    assert report["evidence"]["data"]["sampled_normalized_seconds"] == pytest.approx([0, 0.2, 0.4])
    assert all(record.kind == "frame" for record in evidence_records(report))


def test_missing_native_pts_does_not_fall_back_to_average_fps(media_env, generated_media, media_tools, monkeypatch):
    env = media_env
    item = env.register(generated_media.video)
    original = media_tools.probe_run
    def missing_pts(path, *, timeout, extra=None):
        if extra and "-show_frames" in extra:
            return {"frames": [{"best_effort_timestamp": 0, "best_effort_timestamp_time": "0.0"}]}
        return original(path, timeout=timeout, extra=extra)
    monkeypatch.setattr(media_tools, "probe_run", missing_pts)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-missing-time")
    assert report["status"] == "partial"
    assert report["evidence"]["errors"][0]["code"] == "missing_frame_timestamps"
    assert evidence_records(report) == ()


def test_missing_time_origin_is_not_filled_with_zero(media_env, generated_media, media_tools, monkeypatch):
    env = media_env
    item = env.register(generated_media.video)
    original = media_tools.probe_run
    def missing_origin(path, *, timeout, extra=None):
        result = original(path, timeout=timeout, extra=extra)
        if not extra:
            result["format"].pop("start_time", None)
        return result
    monkeypatch.setattr(media_tools, "probe_run", missing_origin)
    report = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-missing-origin")
    assert report["probe"]["data"]["start_seconds"] is None
    assert report["status"] == "partial"
    assert report["evidence"]["errors"][0]["code"] == "unknown_time_origin"


def test_failed_audio_probe_retries_without_reextracting_good_frames(media_env, generated_media, media_tools, monkeypatch):
    env = media_env
    item = env.register(generated_media.mixed)
    original = media_tools.ffmpeg_run
    def failed_audio(args, **kwargs):
        if "f32le" in args:
            raise MediaError("tool_timeout", "synthetic injected audio timeout")
        return original(args, **kwargs)
    monkeypatch.setattr(media_tools, "ffmpeg_run", failed_audio)
    partial = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-audio-retry")
    assert partial["status"] == "partial" and partial["probe"]["errors"][0]["code"] == "tool_timeout"
    monkeypatch.setattr(media_tools, "ffmpeg_run", original)
    repaired = env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-audio-retry", attempt=2)
    assert repaired["status"] == "succeeded"
    assert repaired["probe"]["cache_hit"] is False and repaired["evidence"]["cache_hit"] is True


def test_linked_job_directory_cannot_redirect_outputs(media_env, generated_media, tmp_path):
    env = media_env
    item = env.register(generated_media.video)
    outside = tmp_path / "outside-job"
    outside.mkdir()
    (env.workspace / "jobs").mkdir()
    link = env.workspace / "jobs/synthetic-linked"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(UnsafePath):
            env.analyzer.analyze(item.asset_id, principal=env.actor, job_id="synthetic-linked")
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(link) if os.name == "nt" else link.unlink()
