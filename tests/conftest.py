from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep tests away from the real ~/.bear and ~/.bear-code state."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BEAR_MCP_TRUSTED", raising=False)
    from agents.policy import reset_permission_cache

    reset_permission_cache()
    yield home
    reset_permission_cache()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return tmp_path
