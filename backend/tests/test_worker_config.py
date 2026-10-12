"""Worker-related runtime settings (``VMAKER_WORKER_*``)."""

from __future__ import annotations

from backend.config import get_settings


def test_worker_settings_follow_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("VMAKER_WORKER_ENABLED", "1")
    monkeypatch.setenv("VMAKER_WORKER_POLL_SECONDS", "0.05")
    settings = get_settings()
    assert settings.worker_enabled is True
    assert settings.worker_poll_seconds == 0.05


def test_worker_can_be_switched_off(monkeypatch) -> None:
    for raw in ("0", "false", "FALSE", "no", "off", " off "):
        monkeypatch.setenv("VMAKER_WORKER_ENABLED", raw)
        assert get_settings().worker_enabled is False, raw

    # Anything that is not a recognised "off" value keeps the queue drained.
    monkeypatch.setenv("VMAKER_WORKER_ENABLED", "yes")
    assert get_settings().worker_enabled is True
    monkeypatch.setenv("VMAKER_WORKER_ENABLED", "maybe")
    assert get_settings().worker_enabled is True


def test_invalid_poll_interval_falls_back_to_the_default(monkeypatch) -> None:
    for raw in ("0", "-1", "abc", "", "1e999"):
        monkeypatch.setenv("VMAKER_WORKER_POLL_SECONDS", raw)
        assert get_settings().worker_poll_seconds == 1.0, raw
