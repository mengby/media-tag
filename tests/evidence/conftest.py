from pathlib import Path
from types import SimpleNamespace
import wave

from PIL import Image
import pytest

from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.templates import TemplateBundle
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import MediaAnalyzer
from media_tag.workers.probe import Toolchain


@pytest.fixture(scope="session")
def media_tools():
    return Toolchain()


@pytest.fixture(scope="session")
def generated_media(tmp_path_factory, media_tools):
    root = tmp_path_factory.mktemp("synthetic-media")
    base = root / "video.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=80x48:rate=5:duration=1.6",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(base)], timeout=20)
    rotated = root / "rotated.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-display_rotation", "90", "-i", str(base), "-c", "copy", "-y", str(rotated)], timeout=20)
    vfr = root / "vfr.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=80x48:rate=10:duration=2",
                            "-vf", "select='not(mod(n,3))'", "-fps_mode", "vfr", "-c:v", "libx264", "-bf", "0", "-y", str(vfr)], timeout=20)
    offset = root / "offset.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-i", str(base), "-c", "copy", "-output_ts_offset", "5", "-y", str(offset)], timeout=20)
    mixed = root / "silent-track.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-i", str(base), "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                            "-shortest", "-c:v", "copy", "-c:a", "aac", "-y", str(mixed)], timeout=20)
    opaque = root / "opaque.png"
    Image.new("RGB", (20, 12), "red").save(opaque)
    transparent = root / "transparent.png"
    image = Image.new("RGBA", (20, 12), (255, 0, 0, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    image.save(transparent)
    exif = root / "exif.jpg"
    metadata = Image.Exif()
    metadata[274] = 6
    Image.new("RGB", (20, 12), "red").save(exif, exif=metadata)
    animated = root / "opaque.gif"
    frames = [Image.new("RGB", (20, 12), color) for color in ("red", "green", "blue")]
    frames[0].save(animated, save_all=True, append_images=frames[1:], duration=200, loop=0, optimize=False)
    silent = root / "silent.wav"
    sound = root / "sound.wav"
    for path, value in ((silent, 0), (sound, 10000)):
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(int(value).to_bytes(2, "little", signed=True) * 16000)
    long_video = root / "long.mp4"
    media_tools.ffmpeg_run(["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=32x24:rate=2:duration=80",
                            "-c:v", "libx264", "-y", str(long_video)], timeout=20)
    multi = root / "multiple-tracks.mka"
    media_tools.ffmpeg_run(["-v", "error", "-i", str(silent), "-i", str(sound), "-map", "0:a", "-map", "1:a",
                            "-c:a", "pcm_s16le", "-y", str(multi)], timeout=20)
    cover = root / "cover.mp3"
    media_tools.ffmpeg_run(["-v", "error", "-i", str(sound), "-i", str(opaque), "-map", "0:a", "-map", "1:v",
                            "-c:a", "libmp3lame", "-c:v", "png", "-disposition:v", "attached_pic", "-id3v2_version", "3", "-y", str(cover)], timeout=20)
    corrupt = root / "damaged.mp4"
    corrupt.write_bytes(b"synthetic broken media, not a real clip")
    return SimpleNamespace(root=root, video=base, rotated=rotated, vfr=vfr, offset=offset, mixed=mixed,
                           opaque=opaque, transparent=transparent, exif=exif, animated=animated,
                           silent=silent, sound=sound, corrupt=corrupt, long_video=long_video, multi=multi, cover=cover)


@pytest.fixture
def media_env(tmp_path, media_tools):
    originals, objects, workspace = tmp_path / "originals", tmp_path / "objects", tmp_path / "workspace"
    for path in (originals, objects, workspace):
        path.mkdir()
    contract = TemplateBundle("synthetic-v1", "d" * 64, (), (), (("synthetic-template", ()),), True)
    catalog = Catalog(Database(tmp_path / "synthetic.sqlite3"), FilesystemBackend(objects, writable=True), contract=contract)
    actor = Principal(actor_id="synthetic-media-editor")
    def register(path, key="synthetic.bin"):
        (originals / key).write_bytes(Path(path).read_bytes())
        return catalog.register_file(FilesystemBackend(originals), key, source_backend_id="synthetic-originals",
                                     principal=actor, request_id="synthetic-request", name="Synthetic media only")
    return SimpleNamespace(catalog=catalog, actor=actor, originals=originals, objects=objects, workspace=workspace,
                           analyzer=MediaAnalyzer(catalog, workspace, media_tools), register=register)
