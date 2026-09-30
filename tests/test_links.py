import base64
from datetime import UTC, datetime, timedelta

import pytest

from jobhunt.links import LinkClaim, LinkConfigError, LinkError, link_config, link_url, sign, verify

SECRET = "k" * 32
NOW = datetime(2026, 9, 30, 13, 30, tzinfo=UTC)
UID = "greenhouse:acme:123"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.mark.parametrize("action", ["approve", "skip"])
def test_round_trip(action):
    token = sign(UID, action, secret=SECRET, now=NOW)
    claim = verify(token, secret=SECRET, now=NOW + timedelta(days=13))
    assert claim == LinkClaim(UID, action, NOW + timedelta(days=14))


def test_token_is_url_safe_and_uid_may_contain_separator():
    uid = "lever:acme:a|b/c+d"
    token = sign(uid, "approve", secret=SECRET.encode(), now=NOW)
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
    assert verify(token, secret=SECRET, now=NOW).uid == uid


def test_tampered_payload_is_rejected():
    token = sign(UID, "skip", secret=SECRET, now=NOW)
    payload, mac = token.split(".")
    forged = unb64(payload).replace(b"|skip|", b"|approve|")
    with pytest.raises(LinkError, match="signature"):
        verify(f"{b64(forged)}.{mac}", secret=SECRET, now=NOW)


def test_tampered_mac_is_rejected():
    token = sign(UID, "approve", secret=SECRET, now=NOW)
    payload, mac = token.split(".")
    raw = bytearray(unb64(mac))
    raw[0] ^= 1
    with pytest.raises(LinkError, match="signature"):
        verify(f"{payload}.{b64(bytes(raw))}", secret=SECRET, now=NOW)


def test_wrong_secret_is_rejected():
    token = sign(UID, "approve", secret=SECRET, now=NOW)
    with pytest.raises(LinkError, match="signature"):
        verify(token, secret="z" * 32, now=NOW)


def test_expired_token_is_rejected():
    token = sign(UID, "approve", secret=SECRET, now=NOW, ttl=timedelta(hours=1))
    assert verify(token, secret=SECRET, now=NOW + timedelta(minutes=59))
    with pytest.raises(LinkError, match="expired"):
        verify(token, secret=SECRET, now=NOW + timedelta(hours=1))


def test_unknown_action_is_rejected_on_sign_and_verify():
    with pytest.raises(ValueError):
        sign(UID, "apply", secret=SECRET, now=NOW)
    # A correctly signed token with an action we don't know (e.g. from a future version).
    import hashlib
    import hmac

    exp = int((NOW + timedelta(days=1)).timestamp())
    payload = f"{UID}|apply|{exp}".encode()
    mac = hmac.new(SECRET.encode(), b"jobhunt-link-v1|" + payload, hashlib.sha256).digest()
    with pytest.raises(LinkError, match="unknown action"):
        verify(f"{b64(payload)}.{b64(mac)}", secret=SECRET, now=NOW)


def test_mac_is_domain_separated():
    """A plain HMAC of the payload (no `jobhunt-link-v1|` prefix) is not a valid token."""
    import hashlib
    import hmac

    exp = int((NOW + timedelta(days=1)).timestamp())
    payload = f"{UID}|approve|{exp}".encode()
    mac = hmac.new(SECRET.encode(), payload, hashlib.sha256).digest()
    with pytest.raises(LinkError, match="signature"):
        verify(f"{b64(payload)}.{b64(mac)}", secret=SECRET, now=NOW)


def test_short_secret_is_refused():
    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        sign(UID, "approve", secret="k" * 31)
    token = sign(UID, "approve", secret=SECRET, now=NOW)
    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        verify(token, secret=b"short")


@pytest.mark.parametrize(
    "token",
    ["", "no-dot", ".", "abc.", ".abc", "a!b.c", "abc.d$f", "a.b.c", "abcde.abc", "é.abc"],
)
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(LinkError):
        verify(token, secret=SECRET, now=NOW)


def test_malformed_base64_names_the_reason():
    with pytest.raises(LinkError, match="malformed base64"):
        verify("a!b.cd", secret=SECRET, now=NOW)


def test_link_config_needs_both_variables():
    assert link_config({}) is None
    assert link_config({"JOBHUNT_PUBLIC_URL": "https://x.test"}) is None
    assert link_config({"JOBHUNT_LINK_SECRET": SECRET}) is None
    assert link_config(
        {"JOBHUNT_PUBLIC_URL": "https://x.test/", "JOBHUNT_LINK_SECRET": SECRET}
    ) == (
        "https://x.test",
        SECRET.encode(),
    )
    with pytest.raises(LinkConfigError, match="JOBHUNT_LINK_SECRET is shorter"):
        link_config({"JOBHUNT_PUBLIC_URL": "https://x.test", "JOBHUNT_LINK_SECRET": "short"})


@pytest.mark.parametrize(
    "url", ["x.test", "ftp://x.test", "https://", "https://x.test/?a=1", "https://x.test/#f"]
)
def test_link_config_rejects_bad_public_url(url):
    with pytest.raises(LinkConfigError, match="JOBHUNT_PUBLIC_URL is not an http"):
        link_config({"JOBHUNT_PUBLIC_URL": url, "JOBHUNT_LINK_SECRET": SECRET})


def test_link_url():
    assert link_url("https://x.test/", "tok") == "https://x.test/a/tok"
