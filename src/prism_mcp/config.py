"""Runtime configuration for the Prism MCP server.

Resolves credentials, cache directory, registry URL, and package
coordinate from environment variables. Defaults follow PRD section 6:

* Registry base: ``canaveral-npm`` (not the deprecated
  ``canaveral-npm-virtual``).
* Cache root: ``~/.cache/prism-mcp/`` per PRD section 5.

All env-var contracts are honored by ``ServerConfig.from_env()``; nothing
else in the codebase should call ``os.environ`` directly.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGISTRY_BASE_URL = (
    "https://artifactory.dyn.ntnxdpro.com/artifactory/api/npm/canaveral-npm/"
)
DEFAULT_PACKAGE_NAME = "@nutanix-ui/prism-reactjs"
DEFAULT_CACHE_SUBDIR = ".cache/prism-mcp"

# Environment-variable names. Centralized so tests can monkeypatch one
# place rather than chasing string literals across modules.
ENV_REGISTRY_URL = "PRISM_MCP_REGISTRY_URL"
ENV_PACKAGE_NAME = "PRISM_MCP_PACKAGE_NAME"
ENV_CACHE_DIR = "PRISM_MCP_CACHE_DIR"
ENV_CA_BUNDLE = "PRISM_MCP_CA_BUNDLE"
ENV_INSECURE_TLS = "PRISM_MCP_INSECURE_TLS"
ENV_JFROG_AUTH = "JFROG_AUTH"
ENV_JFROG_EMAIL = "JFROG_EMAIL"
ENV_JFROG_API_KEY = "JFROG_API_KEY"


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class ServerConfig:
    """Resolved server configuration.

    Args:
        registry_base_url (str): npm registry root, slash-terminated.
        package_name (str): scoped package coordinate to fetch.
        cache_dir (Path): on-disk cache root.
        auth_header (str | None): full ``Authorization`` header value
            (``"Basic <base64>"``) or ``None`` if credentials were not
            supplied. The orchestrator decides whether to error or fall
            back to cache.
        ca_bundle (Path | None): optional path to a PEM file passed to
            httpx as ``verify=<path>``. Use this when the Artifactory
            host presents a TLS chain rooted in an internal CA (the
            common case on Nutanix laptops, where the corporate CA is
            in the macOS Keychain but not in Python's certifi bundle).
            ``None`` means "use certifi's bundle".
        insecure_tls (bool): when ``True``, httpx is told to skip TLS
            verification (``verify=False``). This is an opt-in escape
            hatch for hackathon-scale work on a known-internal
            hostname; do NOT enable it for any non-Nutanix endpoint.
            Mutually exclusive with ``ca_bundle`` (CA bundle wins if
            both are set).
    """

    registry_base_url: str
    package_name: str
    cache_dir: Path
    auth_header: str | None
    ca_bundle: Path | None = None
    insecure_tls: bool = False

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
    ) -> ServerConfig:
        """Build a :class:`ServerConfig` from process environment.

        Args:
            env (dict[str, str] | None): override env mapping (used by
                tests). ``None`` reads from ``os.environ``.

        Returns:
            ServerConfig: a fully resolved config; auth may still be
            ``None`` if no credentials are present in the environment.

        Raises:
            ConfigError: if ``JFROG_EMAIL`` is set without
                ``JFROG_API_KEY`` (or vice versa) — a half-configured
                credential is almost always a bug, not an opt-out.
        """
        env = dict(env) if env is not None else dict(os.environ)

        registry_url = env.get(ENV_REGISTRY_URL, DEFAULT_REGISTRY_BASE_URL)
        if not registry_url.endswith("/"):
            registry_url += "/"

        package_name = env.get(ENV_PACKAGE_NAME, DEFAULT_PACKAGE_NAME)

        cache_override = env.get(ENV_CACHE_DIR)
        cache_dir = (
            Path(cache_override).expanduser()
            if cache_override
            else Path.home() / DEFAULT_CACHE_SUBDIR
        )

        auth_header = _resolve_auth_header(env)

        ca_override = env.get(ENV_CA_BUNDLE, "").strip()
        ca_bundle = Path(ca_override).expanduser() if ca_override else None
        if ca_bundle is not None and not ca_bundle.is_file():
            raise ConfigError(
                f"{ENV_CA_BUNDLE}={ca_bundle} does not exist or is "
                f"not a regular file"
            )

        insecure_tls = _resolve_insecure_tls(env.get(ENV_INSECURE_TLS))

        return cls(
            registry_base_url=registry_url,
            package_name=package_name,
            cache_dir=cache_dir,
            auth_header=auth_header,
            ca_bundle=ca_bundle,
            insecure_tls=insecure_tls,
        )


def _resolve_insecure_tls(raw: str | None) -> bool:
    """Parse ``PRISM_MCP_INSECURE_TLS`` into a strict boolean.

    Accepts the conventional truthy strings (``1``, ``true``, ``yes``,
    ``on``) case-insensitively. Anything else — including ``""`` — is
    treated as ``False``. We're deliberately strict here because this
    knob disables TLS verification; "any non-empty value means true"
    is too easy to set accidentally.
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_auth_header(env: dict[str, str]) -> str | None:
    """Pick the right ``Authorization`` value from JFROG env vars.

    Precedence (per PRD section 6 Decision on Artifactory auth):

    1. ``JFROG_AUTH``: assumed to be raw base64 of ``email:apikey`` and
       wrapped in ``"Basic "``. (PRD Open Question #1 flags the format;
       v1 default is raw base64 — if a caller sets it to a pre-prefixed
       value we accept that as well, just to be defensive.)
    2. ``JFROG_EMAIL`` + ``JFROG_API_KEY``: base64-encode and wrap.

    Args:
        env (dict[str, str]): environment mapping.

    Returns:
        str | None: full Authorization header value, or ``None`` when no
        credentials are configured.

    Raises:
        ConfigError: if only one of email/api-key is set.
    """
    raw_auth = env.get(ENV_JFROG_AUTH, "").strip()
    if raw_auth:
        if raw_auth.lower().startswith("basic "):
            return raw_auth
        return f"Basic {raw_auth}"

    email = env.get(ENV_JFROG_EMAIL, "").strip()
    api_key = env.get(ENV_JFROG_API_KEY, "").strip()

    if bool(email) ^ bool(api_key):
        raise ConfigError(
            "JFROG_EMAIL and JFROG_API_KEY must be set together "
            "(or use JFROG_AUTH instead)"
        )

    if not email:
        return None

    encoded = base64.b64encode(f"{email}:{api_key}".encode()).decode("ascii")
    return f"Basic {encoded}"
