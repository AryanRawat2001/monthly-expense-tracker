"""A dead refresh token (routine: Testing-mode OAuth apps expire them after
7 days) must fall back to a fresh consent flow, not crash the sync."""
import json

import gmail_sync
from google.auth.exceptions import RefreshError


class _StaleCreds:
    valid = False
    expired = True
    refresh_token = "stale"

    def refresh(self, request):
        raise RefreshError("invalid_grant: Bad Request",
                           {"error": "invalid_grant"})


class _FreshCreds:
    valid = True

    def to_json(self):
        return json.dumps({"token": "fresh"})


class _FakeFlow:
    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls()

    def run_local_server(self, port=0):
        return _FreshCreds()


def test_invalid_grant_falls_back_to_consent_flow(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"token": "old"}')
    cred = tmp_path / "credentials.json"
    cred.write_text("{}")

    monkeypatch.setattr(gmail_sync, "TOKEN_FILE", token)
    monkeypatch.setattr(gmail_sync, "CRED_FILE", cred)
    monkeypatch.setattr(gmail_sync.Credentials, "from_authorized_user_file",
                        staticmethod(lambda *a, **k: _StaleCreds()))
    monkeypatch.setattr(gmail_sync, "InstalledAppFlow", _FakeFlow)
    monkeypatch.setattr(gmail_sync, "build",
                        lambda *a, **k: "SERVICE")

    assert gmail_sync._get_service() == "SERVICE"
    assert json.loads(token.read_text()) == {"token": "fresh"}   # re-saved
    assert (token.stat().st_mode & 0o777) == 0o600


def test_corrupt_token_file_falls_back(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("not json at all")
    cred = tmp_path / "credentials.json"
    cred.write_text("{}")

    monkeypatch.setattr(gmail_sync, "TOKEN_FILE", token)
    monkeypatch.setattr(gmail_sync, "CRED_FILE", cred)

    def _boom(*a, **k):
        raise ValueError("bad token file")
    monkeypatch.setattr(gmail_sync.Credentials, "from_authorized_user_file",
                        staticmethod(_boom))
    monkeypatch.setattr(gmail_sync, "InstalledAppFlow", _FakeFlow)
    monkeypatch.setattr(gmail_sync, "build", lambda *a, **k: "SERVICE")

    assert gmail_sync._get_service() == "SERVICE"
