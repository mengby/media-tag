from hashlib import sha256
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.variants.service import VariantService
from router.conftest import env
from upload.conftest import intake, tools
from retime.conftest import retiming


def clients(env, **services):
    env.reader = Principal(actor_id='synthetic-reader', role='reader')
    env.stranger = Principal(actor_id='synthetic-stranger')
    tokens = {'owner':env.owner, 'reader':env.reader, 'stranger':env.stranger, 'worker':env.workers[0]}
    app = create_app(env.router, {sha256(k.encode()).hexdigest():v for k,v in tokens.items()},
        uploads=env.uploads, variants=VariantService(env.router,env.uploads.tools), web=True, **services)
    env.web = app.state.library
    env.app = app
    env.clients = SimpleNamespace(**{key:TestClient(app, headers={'Authorization':'Bearer '+key}) for key in tokens})
    return env


@pytest.fixture
def personnel(intake):
    return clients(intake)


@pytest.fixture
def retime_web(retiming):
    return clients(retiming, retiming=retiming.retimes)
