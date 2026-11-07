from hashlib import sha256
import json
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn

from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.router.models import JobSpec
from .conftest import result, step, submit


class TestClient:
    __test__ = False

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://synthetic.local") as client:
                return await client.request(method, url, **kwargs)
        return anyio.run(run)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)


def configured(env):
    # Test-only tokens. Production supplies private random credentials, never these fixtures.
    bindings = {"synthetic-owner-token": env.owner, "synthetic-worker-token": env.workers[0],
                "synthetic-other-token": Principal(actor_id="synthetic-other"), "synthetic-admin-token": env.admin}
    return create_app(env.router, {sha256(token.encode()).hexdigest(): actor for token, actor in bindings.items()})


def auth(kind="owner"):
    return {"Authorization": f"Bearer synthetic-{kind}-token"}


def test_authenticated_end_to_end_and_role_boundaries(env):
    with TestClient(configured(env)) as client:
        spec = JobSpec(asset_id=env.asset.asset_id, steps=(step(),)).model_dump(mode="json")
        assert client.post("/jobs", json={"request_id": "request", "spec": spec}).status_code == 401
        assert client.post("/jobs", headers=auth("worker"), json={"request_id": "request", "spec": spec}).status_code == 403
        created = client.post("/jobs", headers=auth(), json={"request_id": "request", "spec": spec})
        assert created.status_code == 200
        job = created.json()
        assert client.get(f"/jobs/{job['job_id']}", headers=auth("other")).status_code == 403
        assert client.post("/worker/claim", headers=auth()).status_code == 403
        assert client.post("/admin/recover", headers=auth("worker")).status_code == 403
        assert client.post("/admin/dispatch", headers=auth(), json={"paused": True}).status_code == 403
        claimed = client.post("/worker/claim", headers=auth("worker")).json()
        from media_tag.router.models import Lease
        lease = Lease.model_validate_json(json.dumps(claimed))
        attempt = {"task_id": lease.task.task_id, "lease_epoch": 1}
        assert client.post("/worker/heartbeat", headers=auth("worker"), json=attempt).json()["alive"]
        payload = result(env, lease).model_dump(mode="json")
        assert client.post("/worker/result", headers=auth("worker"), json=payload).json()["accepted"]
        assert client.post("/worker/result", headers=auth("worker"), json=payload).json()["duplicate"]
        assert client.post("/worker/release", headers=auth("worker"), json=attempt).json()["released"]
        assert client.get(f"/jobs/{job['job_id']}", headers=auth()).json()["state"] == "succeeded"
        for path in ("/docs", "/openapi.json", "/review/confirm", "/publish", "/jobs/private/output.png"):
            assert client.get(path, headers=auth("worker")).status_code == 404


def test_request_spoofing_unknown_fields_redaction_and_limits(env):
    app = configured(env)
    with TestClient(app) as client:
        payload = {"request_id": "request", "spec": {}, "role": "admin", "token": "synthetic-secret-to-redact"}
        response = client.post("/jobs", headers=auth(), json=payload)
        assert response.status_code == 422 and "synthetic-secret-to-redact" not in response.text
        assert client.post("/worker/result", headers=auth("worker"), content='{"token":"synthetic-secret-to-redact"}').status_code == 422
        assert client.post("/jobs", headers=auth(), content=b"x" * (1024 * 1024 + 1)).status_code == 413
        assert client.get("/jobs/absent", headers=auth()).status_code == 404
        assert client.get("/jobs/absent", headers={"Authorization": "Bearer invalid-secret"}).status_code == 401
    with pytest.raises(ValueError):
        create_app(env.router, {"raw-secret": env.admin})


def test_http_pause_replan_cancel_and_optimistic_revision(env):
    job = submit(env)
    with TestClient(configured(env)) as client:
        url = f"/jobs/{job['job_id']}"
        assert client.post(url + "/pause", headers=auth(), json={"paused": True}).status_code == 200
        assert client.post("/worker/claim", headers=auth("worker")).json() is None
        patch = {"expected_revision": 1, "steps": [step(config={"batch": 2}).model_dump(mode="json")]}
        assert client.post(url + "/replan", headers=auth(), json=patch).json()["revision"] == 2
        assert client.post(url + "/replan", headers=auth(), json=patch).status_code == 409
        assert client.post(url + "/cancel", headers=auth()).json()["cancel_requested"]


def test_real_loopback_uvicorn_smoke(env):
    job = submit(env)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(configured(env), host="127.0.0.1", port=port, access_log=False, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            assert client.get(f"/jobs/{job['job_id']}", headers=auth()).json()["state"] == "queued"
            assert client.post("/worker/claim", headers=auth("worker")).json()["task"]["attempt"] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        listener.close()
        assert not thread.is_alive()
