# Copyright 2026 Austin Probe
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for secllm.auth — the admin-plane SSO helpers (PyJWT + cryptography; no live IdP).

Exercises PKCE, the HS256 session/flow cookies (incl. that one can't be replayed as the other),
JWKS bearer / id_token verification against a locally-generated RSA keypair (alg-confusion +
audience/nonce rejections), and the admin-group gate. auth.py reads env at import, so the fixture
sets the OIDC env, reloads the module, injects a fake JWKS, and reloads back to "off" on teardown.
"""

import base64
import hashlib
import importlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from secllm import auth as _auth

_OIDC_ENV = {
    "SECLLM_OIDC_ISSUER": "https://secsso.test",
    "SECLLM_OIDC_CLIENT_ID": "secllm",
    "SECLLM_OIDC_CLIENT_SECRET": "shh",
    "SECLLM_PUBLIC_URL": "https://secllm.test",
    "SECLLM_SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "SECLLM_ADMIN_GROUP": "secllm-admins",
}

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key()


class _FakeJWKS:
    """Stand-in for PyJWKClient — returns our test public key for any token, so verify_bearer /
    _verify_id_token validate against the keypair below without a live JWKS endpoint."""

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": _PUB})()


@pytest.fixture()
def auth(monkeypatch):
    for k, v in _OIDC_ENV.items():
        monkeypatch.setenv(k, v)
    importlib.reload(_auth)  # re-read env so auth_enabled/sso_ready/etc. reflect it
    _auth._jwk_client = _FakeJWKS()
    yield _auth
    for k in _OIDC_ENV:
        monkeypatch.delenv(k, raising=False)
    importlib.reload(_auth)  # back to "off" for any other test that imports auth


def _rs256(claims: dict) -> str:
    return jwt.encode(claims, _KEY, algorithm="RS256")


def test_config_enables_auth(auth):
    assert auth.auth_enabled and auth.sso_ready and auth.bearer_ready


def test_is_admin_group_gate(auth):
    assert auth.is_admin(auth.Principal(sub="a", groups=["secllm-admins", "eng"]))
    assert not auth.is_admin(auth.Principal(sub="b", groups=["eng"]))
    assert not auth.is_admin(auth.Principal(sub="c", groups=[]))


def test_is_admin_empty_group_allows_any(auth, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_GROUP", "")  # explicit opt-out: any authenticated user
    assert auth.is_admin(auth.Principal(sub="a", groups=[]))


def test_safe_next_defaults_to_admin(auth):
    assert auth._safe_next("/admin") == "/admin"
    assert auth._safe_next(None) == "/admin"
    assert auth._safe_next("//evil.com") == "/admin"        # protocol-relative
    assert auth._safe_next("https://evil.com") == "/admin"  # absolute
    assert auth._safe_next("/a\r\nSet-Cookie: x") == "/admin"  # header injection


def test_pkce_s256(auth):
    v = auth._new_verifier()
    assert len(v) == 43  # 32 bytes b64url, unpadded
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert auth._challenge_s256(v) == expected


def test_session_roundtrip_and_tamper(auth):
    p = auth.Principal(sub="alice", email="a@x.test", display_name="Alice", groups=["secllm-admins"])
    back = auth.verify_session(auth.mint_session(p))
    assert back.sub == "alice" and back.groups == ["secllm-admins"]
    with pytest.raises(jwt.PyJWTError):
        auth.verify_session(auth.mint_session(p)[:-3] + "AAA")  # tampered signature


def test_flow_session_isolation(auth):
    flow = auth._sign_flow("s1", "v1", "n1", "/admin")
    assert auth._verify_flow(flow)["state"] == "s1"
    with pytest.raises(jwt.PyJWTError):
        auth.verify_session(flow)  # a flow cookie must not verify as a session (disjoint iss/aud)
    with pytest.raises(jwt.PyJWTError):
        auth._verify_flow(auth.mint_session(auth.Principal(sub="a")))  # ...nor the reverse


def test_verify_bearer(auth):
    now = int(time.time())
    good = _rs256({"sub": "alice", "groups": ["secllm-admins"], "iss": "https://secsso.test",
                   "aud": "secllm", "exp": now + 300, "iat": now})
    p = auth.verify_bearer(good)
    assert p.sub == "alice" and auth.is_admin(p)
    for bad in (
        {"sub": "a", "iss": "https://secsso.test", "aud": "other", "exp": now + 300},  # wrong aud
        {"sub": "a", "iss": "https://evil.test", "aud": "secllm", "exp": now + 300},    # wrong iss
        {"sub": "a", "iss": "https://secsso.test", "aud": "secllm", "exp": now - 10},   # expired
    ):
        with pytest.raises(jwt.PyJWTError):
            auth.verify_bearer(_rs256(bad))
    # Alg-confusion: an HS256 token (signed with any secret) must be rejected — RS256 is pinned.
    hs = jwt.encode({"sub": "x", "iss": "https://secsso.test", "aud": "secllm", "exp": now + 300},
                    "any-secret-0123456789abcdef0123456789ab", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        auth.verify_bearer(hs)


def test_verify_id_token_nonce(auth):
    now = int(time.time())
    tok = _rs256({"sub": "alice", "iss": "https://secsso.test", "aud": "secllm",
                  "nonce": "N1", "exp": now + 300})
    assert auth._verify_id_token(tok, "N1").sub == "alice"
    with pytest.raises(jwt.PyJWTError):
        auth._verify_id_token(tok, "N2")  # nonce mismatch


def test_status_shape(auth):
    s = auth.status()
    assert s["auth_enabled"] and s["sso_login"] and s["bearer"]
    assert s["issuer"] == "https://secsso.test" and s["admin_group"] == "secllm-admins"
