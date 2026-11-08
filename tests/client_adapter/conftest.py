from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from search.conftest import sought
from terminology.lexicon.conftest import env
import pytest
from media_tag.workers.probe import Toolchain

@pytest.fixture(scope='session')
def tools():return Toolchain()
