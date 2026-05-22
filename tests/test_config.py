"""Tests for the env-driven config loader."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from prism_mcp.config import (
    DEFAULT_PACKAGE_NAME,
    DEFAULT_REGISTRY_BASE_URL,
    ConfigError,
    ServerConfig,
)


def test_defaults_when_env_is_empty() -> None:
    """No env vars set => defaults from PRD section 6, no auth header."""
    cfg = ServerConfig.from_env(env={})

    assert cfg.registry_base_url == DEFAULT_REGISTRY_BASE_URL
    assert cfg.package_name == DEFAULT_PACKAGE_NAME
    assert cfg.cache_dir.parts[-2:] == (".cache", "prism-mcp")
    assert cfg.auth_header is None


def test_registry_url_is_slash_terminated() -> None:
    """User-supplied URL without trailing slash gets one appended."""
    cfg = ServerConfig.from_env(
        env={"PRISM_MCP_REGISTRY_URL": "https://reg.example/api/npm/x"}
    )
    assert cfg.registry_base_url.endswith("/")


def test_explicit_cache_dir_is_expanded() -> None:
    """``~`` and explicit paths both resolve cleanly."""
    cfg = ServerConfig.from_env(env={"PRISM_MCP_CACHE_DIR": "~/custom"})
    assert str(cfg.cache_dir).endswith("/custom")
    assert "~" not in str(cfg.cache_dir)


def test_jfrog_auth_raw_base64_gets_prefixed() -> None:
    """JFROG_AUTH without ``Basic `` prefix is wrapped automatically."""
    raw = base64.b64encode(b"alice@n.com:KEY").decode("ascii")
    cfg = ServerConfig.from_env(env={"JFROG_AUTH": raw})

    assert cfg.auth_header == f"Basic {raw}"


def test_jfrog_auth_already_prefixed_is_passed_through() -> None:
    """JFROG_AUTH with ``Basic `` prefix is preserved verbatim."""
    raw = base64.b64encode(b"alice@n.com:KEY").decode("ascii")
    cfg = ServerConfig.from_env(env={"JFROG_AUTH": f"Basic {raw}"})

    assert cfg.auth_header == f"Basic {raw}"


def test_email_plus_api_key_composes_basic_header() -> None:
    """Email + api-key are base64-encoded as ``email:apikey``."""
    cfg = ServerConfig.from_env(
        env={"JFROG_EMAIL": "alice@n.com", "JFROG_API_KEY": "SECRET"}
    )

    expected_encoded = base64.b64encode(b"alice@n.com:SECRET").decode("ascii")
    assert cfg.auth_header == f"Basic {expected_encoded}"


def test_partial_email_only_credentials_error() -> None:
    """JFROG_EMAIL without JFROG_API_KEY is a config error."""
    with pytest.raises(ConfigError, match="must be set together"):
        ServerConfig.from_env(env={"JFROG_EMAIL": "alice@n.com"})


def test_partial_api_key_only_credentials_error() -> None:
    """JFROG_API_KEY without JFROG_EMAIL is a config error."""
    with pytest.raises(ConfigError, match="must be set together"):
        ServerConfig.from_env(env={"JFROG_API_KEY": "SECRET"})


def test_jfrog_auth_wins_over_email_pair() -> None:
    """JFROG_AUTH takes precedence over email/api-key."""
    raw = base64.b64encode(b"alice@n.com:KEY").decode("ascii")
    cfg = ServerConfig.from_env(
        env={
            "JFROG_AUTH": raw,
            "JFROG_EMAIL": "bob@n.com",
            "JFROG_API_KEY": "OTHER",
        }
    )

    assert cfg.auth_header == f"Basic {raw}"


def test_ca_bundle_path_is_resolved_when_file_exists(
    tmp_path: Path,
) -> None:
    """``PRISM_MCP_CA_BUNDLE`` resolves to an existing PEM file."""
    pem = tmp_path / "ntnx-ca.pem"
    pem.write_text(
        "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
    )

    cfg = ServerConfig.from_env(env={"PRISM_MCP_CA_BUNDLE": str(pem)})

    assert cfg.ca_bundle == pem
    assert cfg.insecure_tls is False


def test_ca_bundle_missing_path_is_a_config_error(tmp_path: Path) -> None:
    """A pointer to a non-existent file should fail loudly at startup."""
    missing = tmp_path / "no-such.pem"

    with pytest.raises(ConfigError, match="does not exist"):
        ServerConfig.from_env(env={"PRISM_MCP_CA_BUNDLE": str(missing)})


def test_insecure_tls_truthy_values() -> None:
    """``1``, ``true``, ``yes``, ``on`` (any case) enable the escape hatch."""
    for truthy in ("1", "true", "TRUE", "True", "yes", "YES", "on"):
        cfg = ServerConfig.from_env(env={"PRISM_MCP_INSECURE_TLS": truthy})
        assert cfg.insecure_tls is True, truthy


def test_insecure_tls_falsy_values() -> None:
    """Anything not in the truthy set leaves verification on by default."""
    for falsy in ("", "0", "false", "no", "off", "maybe", "  "):
        cfg = ServerConfig.from_env(env={"PRISM_MCP_INSECURE_TLS": falsy})
        assert cfg.insecure_tls is False, falsy


def test_ca_bundle_overrides_insecure_when_both_set(tmp_path: Path) -> None:
    """Explicit trust is recorded even when the escape hatch is on.

    The precedence rule (CA bundle wins) is enforced at the ``server``
    layer in :func:`prism_mcp.server._tls_verify_value`. The config
    layer just records both flags faithfully.
    """
    pem = tmp_path / "ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")

    cfg = ServerConfig.from_env(
        env={
            "PRISM_MCP_CA_BUNDLE": str(pem),
            "PRISM_MCP_INSECURE_TLS": "1",
        }
    )

    assert cfg.ca_bundle == pem
    assert cfg.insecure_tls is True
