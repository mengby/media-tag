from pathlib import Path
import sys

import pytest

from media_tag.workers.probe import MediaError, ProbeOptions, Toolchain, probe_media, run_bounded


def test_tools_are_real_and_identified(media_tools):
    assert media_tools.identity["ffmpeg"]["version"].startswith("ffmpeg version ")
    assert len(media_tools.identity["ffprobe"]["sha256"]) == 64


def test_missing_tool_pair_is_explicit(tmp_path):
    with pytest.raises(MediaError) as error:
        Toolchain(tmp_path)
    assert error.value.code == "tool_missing"


def test_still_images_have_null_duration_and_actual_alpha(media_tools, generated_media):
    opaque = probe_media(generated_media.opaque, media_tools, ProbeOptions())
    transparent = probe_media(generated_media.transparent, media_tools, ProbeOptions())
    assert opaque.kind == transparent.kind == "image"
    assert opaque.duration_seconds is transparent.duration_seconds is None
    assert opaque.transparency["has_transparent_pixels"] is False
    assert transparent.transparency["has_transparent_pixels"] is True
    assert not opaque.has_audio and opaque.audio_tracks == ()


def test_exif_and_video_rotation_are_preserved(media_tools, generated_media):
    picture = probe_media(generated_media.exif, media_tools, ProbeOptions())
    assert (picture.width, picture.height) == (20, 12)
    assert (picture.display_width, picture.display_height) == (12, 20)
    assert picture.raw_metadata["image_metadata"]["exif_orientation"] == 6
    video = probe_media(generated_media.rotated, media_tools, ProbeOptions())
    assert abs(video.rotation_degrees) == 90
    assert (video.display_width, video.display_height) == (48, 80)


def test_gif_duration_and_partial_alpha_coverage(media_tools, generated_media):
    full = probe_media(generated_media.animated, media_tools, ProbeOptions())
    partial = probe_media(generated_media.animated, media_tools, ProbeOptions(max_alpha_frames=1))
    assert full.kind == "gif" and full.duration_seconds == pytest.approx(0.6, abs=0.01)
    assert full.transparency["has_transparent_pixels"] is False
    assert partial.transparency["has_transparent_pixels"] is None
    assert partial.transparency["complete"] is False


def test_audio_presence_is_separate_from_silence(media_tools, generated_media):
    silent = probe_media(generated_media.silent, media_tools, ProbeOptions())
    sound = probe_media(generated_media.sound, media_tools, ProbeOptions())
    assert silent.kind == sound.kind == "audio"
    assert silent.width is None and silent.has_audio
    assert silent.audio_tracks[0]["silent"] is True
    assert sound.audio_tracks[0]["silent"] is False
    mixed = probe_media(generated_media.mixed, media_tools, ProbeOptions())
    assert mixed.kind == "video" and mixed.has_audio
    assert mixed.audio_tracks[0]["sampled_silent"] is True


def test_limited_silent_audio_does_not_assert_whole_track_silence(media_tools, generated_media):
    result = probe_media(generated_media.silent, media_tools, ProbeOptions(audio_scan_seconds=0.1))
    track = result.audio_tracks[0]
    assert track["sampled_silent"] is True and track["silent"] is None
    assert track["complete"] is False and track["scanned_seconds"] <= 0.101


def test_multiple_tracks_and_attached_cover_are_not_misclassified(media_tools, generated_media):
    tracks = probe_media(generated_media.multi, media_tools, ProbeOptions())
    assert tracks.kind == "audio" and len(tracks.audio_tracks) == 2
    assert tracks.audio_tracks[0]["sampled_silent"] is True
    assert tracks.audio_tracks[1]["silent"] is False
    cover = probe_media(generated_media.cover, media_tools, ProbeOptions())
    assert cover.kind == "audio" and cover.video_stream_index is None


def test_alpha_pixel_budget_keeps_unknown_and_still_duration_null(media_tools, generated_media):
    result = probe_media(generated_media.opaque, media_tools, ProbeOptions(max_alpha_pixels=1))
    assert result.transparency["checked_frames"] == 0
    assert result.transparency["has_transparent_pixels"] is None
    assert result.transparency["reason"] == "alpha_pixel_budget"
    assert result.duration_seconds is None


def test_arbitrary_embedded_rotation_text_is_not_written_to_report(media_tools, generated_media, monkeypatch):
    original = media_tools.probe_run
    def embedded_text(path, **kwargs):
        data = original(path, **kwargs)
        data["streams"][0]["tags"] = {"rotate": "https://example.test/file?token=synthetic-secret"}
        return data
    monkeypatch.setattr(media_tools, "probe_run", embedded_text)
    result = probe_media(generated_media.video, media_tools, ProbeOptions())
    assert "synthetic-secret" not in str(result.as_dict())


def test_short_decode_cannot_confirm_full_silence_from_header_duration(media_tools, generated_media, monkeypatch):
    original = media_tools.ffmpeg_run
    def short_pcm(args, **kwargs):
        output, error = original(args, **kwargs)
        return output[:-6400], error  # Synthetic fault: 0.2 seconds missing from 8kHz mono float PCM.
    monkeypatch.setattr(media_tools, "ffmpeg_run", short_pcm)
    track = probe_media(generated_media.silent, media_tools, ProbeOptions()).audio_tracks[0]
    assert track["sampled_silent"] is True and track["silent"] is None
    assert track["complete"] is False


def test_corrupt_media_and_pixel_budget_fail(media_tools, generated_media):
    with pytest.raises(MediaError):
        probe_media(generated_media.corrupt, media_tools, ProbeOptions())
    with pytest.raises(MediaError) as error:
        probe_media(generated_media.opaque, media_tools, ProbeOptions(max_pixels=10))
    assert error.value.code == "pixel_budget"


def test_timeout_and_output_budget_terminate_child():
    with pytest.raises(MediaError) as error:
        run_bounded(Path(sys.executable), ["-c", "import time; time.sleep(2)"], timeout=0.05)
    assert error.value.code == "tool_timeout"
    with pytest.raises(MediaError) as error:
        run_bounded(Path(sys.executable), ["-c", "import sys; sys.stdout.buffer.write(b'x'*1000000)"], timeout=5, stdout_limit=1024)
    assert error.value.code == "output_budget"
