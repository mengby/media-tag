import json

from media_tag.cli import main


def test_media_tool_cli_success(capsys):
    assert main(["check-media-tools"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["ffmpeg"]["sha256"]) == 64


def test_media_tool_cli_missing_is_structured(tmp_path, capsys):
    assert main(["check-media-tools", "--tool-dir", str(tmp_path)]) == 2
    data = capsys.readouterr()
    assert not data.out and json.loads(data.err)["error"] == "tool_missing"
