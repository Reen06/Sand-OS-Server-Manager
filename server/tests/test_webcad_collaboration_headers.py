from app import proxy, registry


def test_webcad_uses_trusted_hub_identity_header():
    assert registry.APPS["webcad"].sso_header == "X-Forwarded-User"


def test_webcad_shared_roots_come_from_nas_membership(monkeypatch):
    monkeypatch.setattr(
        proxy.nas_shares,
        "shares_for",
        lambda user: [
            {"slug": "admin-braeden", "members": ["admin", "braeden"]},
            {"slug": "machine-team", "members": ["admin", "casey"]},
        ],
    )
    assert proxy._webcad_shared_roots("admin") == (
        "shared:admin-braeden,shared:machine-team"
    )
