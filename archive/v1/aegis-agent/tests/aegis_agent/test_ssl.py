"""Tests for SSL setup policy."""

from __future__ import annotations

from aegis_agent.utils import ssl as ssl_utils


def test_setup_ssl_requires_rbc_security_when_verify_enabled(monkeypatch) -> None:
    """SSL_VERIFY=true must not fall back when rbc_security is unavailable."""
    monkeypatch.setattr(ssl_utils.config, "ssl_verify", True)

    def fake_import_module(name: str):
        assert name == "rbc_security"
        raise ImportError("missing rbc_security")

    monkeypatch.setattr(ssl_utils.importlib, "import_module", fake_import_module)

    result = ssl_utils.setup_ssl()

    assert result["success"] is False
    assert result["verify"] is True
    assert "cert_path" not in result
    assert "rbc_security" in result["error"]


def test_setup_ssl_enables_rbc_security_when_verify_enabled(monkeypatch) -> None:
    """SSL_VERIFY=true should enable RBC certificates through rbc_security."""
    calls = []
    monkeypatch.setattr(ssl_utils.config, "ssl_verify", True)

    class FakeRbcSecurity:
        @staticmethod
        def enable_certs() -> None:
            calls.append("enabled")

    monkeypatch.setattr(
        ssl_utils.importlib,
        "import_module",
        lambda name: FakeRbcSecurity,
    )

    result = ssl_utils.setup_ssl()

    assert calls == ["enabled"]
    assert result["success"] is True
    assert result["verify"] is True
    assert "cert_path" not in result
    assert result["decision_details"] == "SSL verification: enabled with rbc_security"
