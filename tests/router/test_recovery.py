from concurrent.futures import ThreadPoolExecutor
import sqlite3
import subprocess
import sys

import pytest

from media_tag.catalog import CatalogError, Database
import media_tag.catalog.database as db_module
from media_tag.contracts.models import TaskLimits
from media_tag.router.models import Lease
from media_tag.router.service import Router
from .conftest import finish, result, step, submit


def make_v1(path):
    connection = sqlite3.connect(path)
    for statement in db_module.SCHEMA:
        connection.execute(statement)
    connection.execute("INSERT INTO schema_migrations VALUES(1,?,'synthetic-date')", (db_module.SCHEMA_CHECKSUM,))
    connection.execute("INSERT INTO catalog_metadata VALUES('synthetic-key','synthetic-value')")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()


def test_v1_upgrade_preserves_data_and_failed_v2_is_transactional(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-v1.sqlite3"
    make_v1(path)
    database = Database(path)
    original = db_module.ROUTER_SCHEMA
    monkeypatch.setattr(db_module, "ROUTER_SCHEMA", (*original, "synthetic invalid SQL"))
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='router_jobs'").fetchone()
        assert connection.execute("SELECT value FROM catalog_metadata").fetchone()[0] == "synthetic-value"
    monkeypatch.setattr(db_module, "ROUTER_SCHEMA", original)
    database.initialize()
    with database.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT value FROM catalog_metadata").fetchone()[0] == "synthetic-value"
        connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=2")
    with pytest.raises(CatalogError, match="checksum"):
        database.initialize()


CHILD = '''
import sys, time
from pathlib import Path
from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.templates import TemplateBundle
from media_tag.contracts.models import ContractModel
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.router.models import RouterOptions, StageDefinition
from media_tag.router.service import Router
class Config(ContractModel):
    batch: int = 4
catalog = Catalog(Database(Path(sys.argv[1])), FilesystemBackend(Path(sys.argv[2]), writable=True),
    contract=TemplateBundle("synthetic-v1", "f"*64, (), (), (("synthetic-template", ()),), True))
router = Router(catalog, Path(sys.argv[3]), {"synthetic": StageDefinition(Config,"synthetic-v1",gpu_allowed=True)},
    options=RouterOptions(lease_seconds=10.0,heartbeat_seconds=2.0), clock=lambda:1000.0)
lease = router.claim(principal=Principal(actor_id="synthetic-worker-0",role="worker"))
print(lease.model_dump_json(),flush=True)
while True:
    time.sleep(1)
'''


def test_killed_worker_and_router_restart_fence_old_attempt_and_hold_gpu(env):
    submit(env, (step(limits=TaskLimits(gpu_slots=1)),))
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(env.database.path), str(env.objects), str(env.workspace)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            line = pool.submit(child.stdout.readline).result(timeout=10)
        assert line, child.stderr.read() if child.poll() is not None else "child did not claim"
        lease = Lease.model_validate_json(line)
        env.now[0] += 13
        restarted = Router(env.catalog, env.workspace, env.router.stages, options=env.router.options, clock=lambda: env.now[0],
                           process_stopped=lambda task, epoch: task == lease.task.task_id and epoch == 1 and child.poll() is not None)
        restarted.recover(principal=env.admin)
        env.now[0] += 3
        assert restarted.claim(principal=env.workers[1]) is None
        assert child.poll() is None  # Expiry alone did not replace a still-running GPU owner.
        child.kill()
        child.wait(timeout=5)
        restarted.confirm_stopped(lease.task.task_id, 1, principal=env.admin)
        newer = restarted.claim(principal=env.workers[1])
        assert newer.task.attempt == 2 and newer.task.lease_epoch == 2
        assert not restarted.complete(result(env, lease), principal=env.workers[0])["accepted"]
        env.router = restarted
        assert finish(env, newer, env.workers[1])["accepted"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
