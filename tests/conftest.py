import pytest


@pytest.fixture(autouse=True)
def _no_network_td_refresh(monkeypatch):
    """Tests never fetch published TD sources from the internet."""
    from spoolmix import service

    monkeypatch.setattr(service, "refresh_published", lambda *a, **k: 0)
    monkeypatch.setenv("SPOOLMIX_AUTO_TD", "0")
