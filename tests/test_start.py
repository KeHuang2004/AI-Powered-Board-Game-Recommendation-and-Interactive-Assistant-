"""Native entrypoint options and browser readiness, without starting an API."""
from types import SimpleNamespace

import pytest

import start


def test_server_options_without_browser(monkeypatch):
    servers = []

    def fake_server(config):
        server = SimpleNamespace(config=config, run=lambda: servers.append(config))
        return server

    monkeypatch.setattr(start.uvicorn, "Server", fake_server)
    monkeypatch.setattr(start.webbrowser, "open", lambda _: pytest.fail("browser must stay closed"))
    start.main(["--host", "0.0.0.0", "--port", "8123", "--no-browser"])
    assert [(config.host, config.port, config.app) for config in servers] == [
        ("0.0.0.0", 8123, "backend.server:app")
    ]


def test_browser_opens_english_home_after_startup(monkeypatch):
    server = SimpleNamespace(started=False, should_exit=False)
    opened = []
    monkeypatch.setattr(start.time, "sleep", lambda _: setattr(server, "started", True))
    monkeypatch.setattr(start.webbrowser, "open", opened.append)
    start.open_when_ready(server, "0.0.0.0", 8123)
    assert opened == ["http://127.0.0.1:8123/en/"]


def test_missing_catalog_help(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="python -m backend.database"):
        start.main(["--no-browser"])
