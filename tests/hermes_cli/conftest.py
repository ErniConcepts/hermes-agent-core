from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_path() -> Path:
    base = Path.cwd() / ".tmp-testdirs"
    base.mkdir(parents=True, exist_ok=True)
    root = base / f"pytest-hermes-cli-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
