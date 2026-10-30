"""Never use an operator's plugin state while testing a package."""

import pytest


@pytest.fixture(autouse=True)
def isolate_plugin_storage(tmp_path, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path / "runtime")
    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "installed")
