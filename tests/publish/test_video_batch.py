from threading import Event, Lock

import pytest

from media_tag.publish.client import measure_stage
from media_tag.publish.video_batch import follow


def test_registrar_progresses_while_first_upload_waits(tmp_path):
    tag_log = tmp_path / "tags-progress.jsonl"
    tag_log.write_text('{"id":"first","ok":true}\n', encoding="utf-8")
    second_arrived = Event()
    completed = []
    entries = []
    journal_lock = Lock()

    class Batch:
        groups = {"first": [], "second": []}
        reg = {"first": "asset1"}

        def register(self, row):
            self.reg[row["id"]] = "asset2"

        def job(self, asset):
            return asset

        def deliver(self, row, asset, job, timings):
            if row["id"] == "first":
                with tag_log.open("a", encoding="utf-8") as stream:
                    stream.write('{"id":"second","ok":true}\n' * 2)
                if not second_arrived.wait(12):
                    raise RuntimeError("second upload blocked behind first")
            else:
                second_arrived.set()
            completed.append(row["id"])
            return {"state": "indexed"}

    def record(path, row):
        with journal_lock:
            entries.append((path.name, row))
        # Fail fast if the independent registration path regresses.
        assert row.get("status") != "error"

    follow(Batch(), tmp_path, lambda _: [], record, production_concurrency=2)
    assert sorted(completed) == ["first", "second"]
    assert sum(row.get("status") == "indexed" for _, row in entries) == 2


def test_failed_publication_records_stage_and_elapsed_time(monkeypatch):
    import media_tag.publish.client as client
    ticks = iter([10, 13, 20, 22])
    monkeypatch.setattr(client, "monotonic", lambda: next(ticks))
    timings = {}
    with measure_stage(timings, "upload_seconds"):
        pass
    with pytest.raises(ValueError, match="503"), measure_stage(timings, "upload_seconds"):
        raise ValueError("503")
    assert timings == {"failed_stage": "upload_seconds", "upload_seconds": 5}
