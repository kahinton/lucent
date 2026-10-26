"""Console configuration, read from the environment.

Two independent secrets are required, and they are deliberately separate:

* ``LUCENT_OPS_API_KEY`` — a hyperadmin API key. It is the console's identity
  towards Lucent and never reaches the browser.
* ``LUCENT_OPS_CONSOLE_TOKEN`` — the passphrase a human types to open the
  console. It guards the *console UI*, not the API.

Keeping them apart means a leaked console passphrase does not leak the API key,
and rotating the API key does not require telling every operator a new
passphrase.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when the console cannot start with the given environment."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


@dataclass(frozen=True)
class ConsoleConfig:
    """Everything the console needs to run."""

    base_url: str
    api_key: str
    console_token: str
    signing_secret: str
    host: str = "127.0.0.1"
    port: int = 8767
    secure_cookies: bool = False
    session_hours: int = 8
    #: Only used to label the UI; a real deployment would look this up.
    instance_label: str = "Local instance"
    #: Extra hosts permitted in the Host header. The default binds to loopback,
    #: where the console is only reachable from this machine.
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def api_root(self) -> str:
        return self.base_url.rstrip("/")


def load_config(env: dict[str, str] | None = None) -> ConsoleConfig:
    """Build the config from the environment, failing loudly on anything missing.

    A missing API key is a hard error rather than a warning: a console that
    starts but cannot do anything is worse than one that refuses to start and
    says which variable to set.
    """
    source = env if env is not None else dict(os.environ)

    def read(name: str, default: str | None = None) -> str | None:
        value = source.get(name)
        if value is None or not str(value).strip():
            return default
        return str(value).strip()

    api_key = read("LUCENT_OPS_API_KEY")
    console_token = read("LUCENT_OPS_CONSOLE_TOKEN")
    base_url = read("LUCENT_OPS_BASE_URL", read("LUCENT_PUBLIC_URL", "http://localhost:8766"))

    missing = []
    if not api_key:
        missing.append("LUCENT_OPS_API_KEY")
    if not console_token:
        missing.append("LUCENT_OPS_CONSOLE_TOKEN")
    if missing:
        raise ConfigError(
            "Missing required environment: "
            + ", ".join(missing)
            + ". Mint a hyperadmin key with `python -m lucent.hyperadmin mint-key` "
            "and choose a console passphrase."
        )

    # The signing secret protects the session cookie. When it is not supplied it
    # is generated per process, which is safe (a restart logs everyone out) and
    # means a local run needs no extra configuration.
    signing_secret = read("LUCENT_OPS_CONSOLE_SECRET") or secrets.token_urlsafe(32)

    allowed = [
        host.strip()
        for host in (read("LUCENT_OPS_ALLOWED_HOSTS") or "localhost,127.0.0.1").split(",")
        if host.strip()
    ]

    return ConsoleConfig(
        base_url=base_url or "http://localhost:8766",
        api_key=api_key or "",
        console_token=console_token or "",
        signing_secret=signing_secret,
        host=read("LUCENT_OPS_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(read("LUCENT_OPS_PORT", "8767") or 8767),
        secure_cookies=(read("LUCENT_OPS_SECURE_COOKIES", "false") or "false").lower()
        in {"1", "true", "yes", "on"},
        session_hours=int(read("LUCENT_OPS_SESSION_HOURS", "8") or 8),
        instance_label=read("LUCENT_OPS_INSTANCE_LABEL", "Local instance") or "Local instance",
        allowed_hosts=tuple(allowed),
    )
