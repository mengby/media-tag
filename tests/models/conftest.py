from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pytest

from router.conftest import env
from upload.conftest import intake, tools
from media_tag.models.registry import ModelRegistry


@pytest.fixture(scope='session')
def registry():
    return ModelRegistry(Path('cache/models/d04').resolve())


@pytest.fixture
def text_image():
    image = Image.new('RGB', (640, 150), 'white')
    ImageDraw.Draw(image).text((10, 30), '测试 ABC 123', fill='black', font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 42))
    return image
