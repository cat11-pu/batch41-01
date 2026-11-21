import json
import re

import pytest

import oauth_client_registration as ocr


def registry(**over):
    args = {
        "allowed_scopes": ["openid", "files.read", "files.write"],
        "allowed_grants": ["authorization_code", "client_credentials", "refresh_token"],
    }
    args.update(over)
    return ocr.DynamicClientRegistry(**args)


def claim(**over):
    base = {
        "software_id": "sw-1",
        "client_name": "Demo",
        "redirect_uris": ["https://app.example.com/cb"],
        "token_endpoint_auth_method": "client_secret_basic",
        "grant_types": ["authorization_code"],
        "scopes": ["openid"],
        "software_statement": {"signature": "sig-ok", "expires_at": 1000},
        "logo_uri": "https://cdn.example.com/logo.png",
    }
    base.update(over)
    return base


def test_register_confidential_client_returns_secret():
    reg = registry()
    result = reg.register(claim(), 0)
    assert re.fullmatch(r"[0-9a-f]{32}", result["client_id"])
    assert re.fullmatch(r"[0-9a-f]{32}", result["client_secret"])
    assert result["idempotent"] is False
    assert reg.stats()["clients"] == 1


def test_repeated_software_id_is_idempotent():
    reg = registry()
    first = reg.register(claim(), 0)
    second = reg.register(claim(client_name="Renamed"), 10)
    assert second["client_id"] == first["client_id"]
    assert second["client_secret"] is None
    assert second["idempotent"] is True
    assert reg.stats()["clients"] == 1


def test_public_client_has_no_secret_and_cannot_use_client_credentials():
    reg = registry()
    result = reg.register(
        claim(
            token_endpoint_auth_method="none",
            grant_types=["authorization_code"],
            scopes=["openid"],
        ),
        0,
    )
    assert result["client_secret"] is None
    with pytest.raises(ValueError):
        reg.issue_token(result["client_id"], "client_credentials", ["openid"], 0)


def test_redirect_requires_https_or_loopback():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(claim(redirect_uris=["http://app.example.com/cb"]), 0)
    loopback = reg.register(
        claim(software_id="sw-loop", redirect_uris=["http://127.0.0.1:8080/cb"]), 0
    )
    assert loopback["redirect_uris"] == ["http://127.0.0.1:8080/cb"]


def test_redirect_fragment_and_wildcard_are_rejected():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(claim(redirect_uris=["https://app.example.com/cb#frag"]), 0)
    with pytest.raises(ValueError):
        reg.register(
            claim(software_id="sw-wild", redirect_uris=["https://*.example.com/cb"]), 0
        )


def test_default_ports_are_normalized():
    reg = registry()
    assert reg.normalize_redirect_uri("https://App.Example.com:443/cb") == "https://app.example.com/cb"
    assert reg.normalize_redirect_uri("http://127.0.0.1:80/cb") == "http://127.0.0.1/cb"
    assert (
        reg.normalize_redirect_uri("https://app.example.com:8443/cb")
        == "https://app.example.com:8443/cb"
    )


def test_machine_grant_requires_scope():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(
            claim(grant_types=["client_credentials"], redirect_uris=[], scopes=[]), 0
        )


def test_scopes_must_be_whitelisted():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(claim(scopes=["openid", "root.all"]), 0)


def test_logo_uri_rejects_internal_hosts():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(claim(logo_uri="http://169.254.169.254/latest/meta-data"), 0)
    with pytest.raises(ValueError):
        reg.register(claim(software_id="sw-logo2", logo_uri="https://10.0.0.5/logo.png"), 0)
    allowed = reg.register(
        claim(software_id="sw-logo3", logo_uri="https://cdn.example.com/logo.png"), 0
    )
    assert allowed["logo_uri"] == "https://cdn.example.com/logo.png"


def test_software_statement_signature_and_expiry():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(
            claim(software_statement={"signature": "forged", "expires_at": 100}), 0
        )
    with pytest.raises(ValueError):
        reg.register(
            claim(
                software_id="sw-old",
                software_statement={"signature": "sig-ok", "expires_at": 50},
            ),
            100,
        )


def test_describe_requires_registration_token():
    reg = registry()
    result = reg.register(claim(), 0)
    info = reg.describe(result["client_id"], result["registration_access_token"])
    assert info["client_name"] == "Demo"
    assert "client_secret" not in info
    with pytest.raises(ValueError):
        reg.describe(result["client_id"], "0" * 32)
    with pytest.raises(ValueError):
        reg.describe("f" * 32, result["registration_access_token"])


def test_rotate_credential_rotates_secret_and_token():
    reg = registry()
    result = reg.register(claim(), 0)
    rotated = reg.rotate_credential(
        result["client_id"], result["registration_access_token"], 10
    )
    assert rotated["client_secret"] != result["client_secret"]
    with pytest.raises(ValueError):
        reg.describe(result["client_id"], result["registration_access_token"])
    assert (
        reg.describe(result["client_id"], rotated["registration_access_token"])["client_id"]
        == result["client_id"]
    )


def test_delete_revokes_refresh_tokens():
    reg = registry()
    result = reg.register(claim(), 0)
    token = reg.issue_token(result["client_id"], "authorization_code", ["openid"], 0)
    reg.delete(result["client_id"], result["registration_access_token"], 10)
    with pytest.raises(ValueError):
        reg.refresh(token["refresh_token"], 11)
    with pytest.raises(ValueError):
        reg.describe(result["client_id"], result["registration_access_token"])


def test_audit_and_describe_never_expose_secret():
    reg = registry()
    result = reg.register(claim(), 0)
    reg.issue_token(result["client_id"], "authorization_code", ["openid"], 0)
    dump = json.dumps(reg.audit())
    assert result["client_secret"] not in dump
    assert "client_secret" not in dump
    assert reg.stats()["audit_events"] >= 2


def test_invalid_claims_and_unknown_client_are_rejected():
    reg = registry()
    with pytest.raises(ValueError):
        reg.register(claim(software_id=""), 0)
    with pytest.raises(ValueError):
        reg.register(claim(software_id="sw-noname", client_name=""), 0)
    with pytest.raises(ValueError):
        reg.register(
            claim(
                software_id="sw-grant",
                grant_types=["password"],
                redirect_uris=[],
            ),
            0,
        )
    with pytest.raises(ValueError):
        reg.issue_token("f" * 32, "authorization_code", ["openid"], 0)
