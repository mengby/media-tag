from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from router.conftest import env
from upload.conftest import tools

from media_tag.similarity.contracts import SourceVersion
from media_tag.similarity.service import AuthorizedSimilarity
from media_tag.storage.filesystem import FilesystemBackend


def picture(*, size=64, shape='circle', color='red', alpha=False, format='PNG'):
    image=Image.new('RGBA' if alpha else 'RGB',(size,size),(0,0,0,0) if alpha else 'white')
    draw=ImageDraw.Draw(image)
    area=(size//6,size//6,size*5//6,size*5//6)
    if shape=='circle':draw.ellipse(area,fill=color)
    else:draw.rectangle(area,fill=color)
    output=BytesIO();image.save(output,format=format)
    return output.getvalue()


def reference(data, asset_id='synthetic-image', revision=1):
    return SourceVersion(asset_id=asset_id,asset_revision=revision,content_sha256=sha256(data).hexdigest())


@pytest.fixture
def authorized(env, tools, tmp_path):
    root=tmp_path/'similarity-originals';root.mkdir();(root/'README.md').write_text('Synthetic original media; read-only catalogue source.\n')
    service=AuthorizedSimilarity(env.router,tools)
    def register(data, *, name='synthetic.png', principal=None):
        principal=principal or env.owner
        path=root/name;path.write_bytes(data)
        asset=env.catalog.register_file(FilesystemBackend(root),name,source_backend_id='synthetic-similarity',
            principal=principal,request_id='source-'+name,name=name)
        return SourceVersion(asset_id=asset.asset_id,asset_revision=asset.revision,content_sha256=asset.sha256)
    return SimpleNamespace(env=env,service=service,register=register,root=root,tmp=tmp_path)
