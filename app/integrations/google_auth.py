"""Google OAuth for the personal agent — desktop InstalledAppFlow, least-privilege
scopes, local token storage.

Scopes are deliberately minimal (Phase 2): Gmail read + compose (drafts only, NEVER
gmail.send), Calendar read-only. Calendar *write* is not requested until Phase 3
(reminder -> event mirroring) so the token literally cannot alter the calendar yet.

Token lives at settings.google_token_path (git-ignored data/); the OAuth client
secret at settings.google_client_secret_path. Both stay on the machine. Keychain-
backed encryption at rest is the Phase 9 (Mac mini) hardening — not built here.

Honest limitation: for a personal @gmail.com with these *sensitive* Gmail scopes, an
unverified app in the OAuth "testing" publishing status issues refresh tokens that
expire after ~7 days, so periodic re-consent is expected. Verifying the app is a
heavy process not worth it for a single user; Phase 9 revisits.
"""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # drafts only — never gmail.send
    "https://www.googleapis.com/auth/calendar.readonly",
]


class GoogleAuthError(RuntimeError):
    pass


def _save_token(creds: Credentials) -> None:
    path = Path(settings.google_token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)  # owner-only: the token is a live credential


def get_credentials() -> Credentials:
    """Return valid Google credentials, refreshing or running the one-time browser
    consent flow as needed. Raises GoogleAuthError if the client secret is missing.
    """
    token_path = Path(settings.google_token_path)
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    secret_path = Path(settings.google_client_secret_path)
    if not secret_path.exists():
        raise GoogleAuthError(
            f"No Google OAuth client secret at {secret_path}. Create a Google Cloud "
            "project, enable the Gmail + Calendar APIs, make a Desktop OAuth client, and "
            "download its JSON to that path. See docs/06-phase2-build.md."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    creds = flow.run_local_server(port=0)  # opens a browser once
    _save_token(creds)
    return creds


def has_token() -> bool:
    """Cheap check used by verify/setup flows: is OAuth already configured?"""
    return Path(settings.google_token_path).exists()
