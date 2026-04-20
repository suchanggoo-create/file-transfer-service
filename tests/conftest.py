import sys
from os import chdir
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    # Use default storage root (Path.cwd() / "data") by ensuring cwd is project root.
    # Uploaded files will persist after tests complete.
    chdir(PROJECT_ROOT)
    return TestClient(app)
