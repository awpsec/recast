import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECAST_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(scope="session")
def library_template(tmp_path_factory):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("libtemplate")
    import make_library
    make_library.main(root)
    return root


@pytest.fixture
def library(library_template, tmp_path):
    dst = tmp_path / "lib"
    shutil.copytree(library_template, dst)
    return dst
