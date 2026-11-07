from hashlib import sha256

import pytest

from router.conftest import env
from upload.conftest import intake, tools
from variants.conftest import variants
from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.files.service import FileService


def app(env):
    bindings = {'owner': env.owner, 'reader': Principal(actor_id=env.owner.actor_id, role='reader'),
                'other': Principal(actor_id='synthetic-file-other', role='reader'), 'worker': env.workers[0], 'admin': env.admin}
    return create_app(env.router, {sha256(f'synthetic-file-{name}'.encode()).hexdigest(): actor for name, actor in bindings.items()},
                      uploads=getattr(env, 'uploads', None), variants=getattr(env, 'variants', None))


def headers(name='reader', **kwargs):
    return {'Authorization': f'Bearer synthetic-file-{name}', **kwargs}


def content(asset, operation='content'):
    return f'/v1/assets/{asset.asset_id}/{operation}?sha256={asset.sha256}'
