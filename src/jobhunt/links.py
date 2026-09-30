"""Signed, expiring Approve/Skip links for the digest email. Standard library only.

A token is ``base64url(payload) + "." + base64url(mac)`` where the payload is
``uid|action|exp`` (``exp`` in Unix seconds) and the MAC is HMAC-SHA256 over
``b"jobhunt-link-v1|" + payload``. The domain prefix keeps these MACs from being valid for
anything else signed with the same secret, and ``v1`` leaves room to change the format.

There's deliberately no digest id in the payload: the digest is recorded only after the
email is sent, so there isn't one yet when the links are rendered.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

ACTIONS: tuple[str, ...] = ("approve", "skip")
DEFAULT_TTL = timedelta(days=14)
MIN_SECRET_BYTES = 32
SECRET_ENV = "JOBHUNT_LINK_SECRET"
PUBLIC_URL_ENV = "JOBHUNT_PUBLIC_URL"

_DOMAIN = b"jobhunt-link-v1|"
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


class LinkError(ValueError):
    """A token that can't be trusted: malformed, tampered with, expired or unknown action."""


@dataclass(frozen=True)
class LinkClaim:
    uid: str
    action: str
    exp: datetime


class LinkConfigError(ValueError):
    """The link settings are unusable. The message names the variable, never its value."""


def secret_bytes(secret: str | bytes) -> bytes:
    """The secret as bytes. Refuses one shorter than 32 bytes (a configuration error,
    raised as `LinkConfigError`, not a `LinkError`)."""
    raw = secret.encode() if isinstance(secret, str) else secret
    if len(raw) < MIN_SECRET_BYTES:
        raise LinkConfigError(f"{SECRET_ENV} is shorter than {MIN_SECRET_BYTES} bytes")
    return raw


def link_config(env: Mapping[str, str] | None = None) -> tuple[str, bytes] | None:
    """``(public_url, secret)`` when both env vars are set, else ``None`` (links off).
    Raises `LinkConfigError` when they are set but unusable."""
    env = os.environ if env is None else env
    base = env.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    secret = env.get(SECRET_ENV, "")
    if not base or not secret:
        return None
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise LinkConfigError(f"{PUBLIC_URL_ENV} is not an http(s) base URL")
    return base, secret_bytes(secret)


def link_url(base: str, token: str) -> str:
    return f"{base.rstrip('/')}/a/{token}"


def sign(
    uid: str,
    action: str,
    *,
    secret: str | bytes,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    key = secret_bytes(secret)
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; must be one of {ACTIONS}")
    exp = int(((now or datetime.now(UTC)) + ttl).timestamp())
    payload = f"{uid}|{action}|{exp}".encode()
    return f"{_b64encode(payload)}.{_b64encode(_mac(key, payload))}"


def verify(token: str, *, secret: str | bytes, now: datetime | None = None) -> LinkClaim:
    """Check the MAC first, then the contents. Raises `LinkError` with a reason."""
    key = secret_bytes(secret)
    payload_part, sep, mac_part = token.partition(".")
    if not sep or not payload_part or not mac_part:
        raise LinkError("malformed token")
    payload = _b64decode(payload_part)
    mac = _b64decode(mac_part)
    if not hmac.compare_digest(mac, _mac(key, payload)):
        raise LinkError("bad signature")

    # Signed by us, so the layout is ours; still parse defensively. rsplit lets the uid
    # itself contain "|".
    try:
        uid, action, exp_text = payload.decode().rsplit("|", 2)
        exp = datetime.fromtimestamp(int(exp_text), UTC)
    except (UnicodeDecodeError, ValueError, OverflowError, OSError) as exc:
        raise LinkError("malformed payload") from exc
    if action not in ACTIONS:
        raise LinkError(f"unknown action {action!r}")
    if (now or datetime.now(UTC)) >= exp:
        raise LinkError("link expired")
    return LinkClaim(uid=uid, action=action, exp=exp)


def _mac(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, _DOMAIN + payload, hashlib.sha256).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    # The stdlib decoder silently skips characters outside the alphabet; be strict instead.
    if not _B64URL.fullmatch(text):
        raise LinkError("malformed base64")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise LinkError("malformed base64") from exc
