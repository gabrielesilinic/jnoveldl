#!/usr/bin/env python3
"""J-Novel Club catalogue browser — auth bootstrap script.

Known working endpoints (labs.j-novel.club/app/v2):
  POST /auth/login          — email + password → bearer token
  POST /auth/logout         — invalidate token
  GET  /me                  — user profile & subscription info
  GET  /me/library          — purchased books (paginated)
  GET  /series              — public catalogue  (paginated)
  GET  /series/{slug}/aggregate — full series metadata w/ volumes & parts
"""

import getpass
import json
import os
import sys
from pathlib import Path

import keyring
import requests

SERVICE_NAME = "jnoveldl"
API_BASE = "https://labs.j-novel.club/app/v2"
LOCAL_STATE_DIR = Path.home() / ".local" / "share" / SERVICE_NAME

BAKED_CREDENTIALS_FILE = LOCAL_STATE_DIR / "credentials.json"

os.makedirs(LOCAL_STATE_DIR, exist_ok=True)

_COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ── credential helpers ──────────────────────────────────────────────
def _load_baked_credentials() -> tuple[str | None, str | None]:
    if not BAKED_CREDENTIALS_FILE.exists():
        return None, None

    try:
        raw = json.loads(BAKED_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None

    username = raw.get("username")
    password = raw.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None, None
    return username, password


def save_baked_credentials(username: str, password: str) -> Path:
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"username": username, "password": password}
    tmp_path = BAKED_CREDENTIALS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")

    # Best-effort local hardening for plaintext credential storage.
    if not sys.platform.startswith("win"):
        os.chmod(LOCAL_STATE_DIR, 0o700)
        os.chmod(tmp_path, 0o600)
    tmp_path.replace(BAKED_CREDENTIALS_FILE)
    return BAKED_CREDENTIALS_FILE


def bake_credentials_from_keyring() -> Path:
    try:
        username = keyring.get_password(SERVICE_NAME, "username")
        password = keyring.get_password(SERVICE_NAME, "password")
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringError) as exc:
        raise RuntimeError(f"Keyring is unavailable: {exc}") from exc

    if not username or not password:
        raise RuntimeError("No credentials found in keyring to bake.")

    return save_baked_credentials(username, password)


def get_stored_credentials() -> tuple[str | None, str | None]:
    try:
        username = keyring.get_password(SERVICE_NAME, "username")
        password = keyring.get_password(SERVICE_NAME, "password")
        if username and password:
            return username, password
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringError):
        pass

    return _load_baked_credentials()


def store_credentials(username: str, password: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, "username", username)
        keyring.set_password(SERVICE_NAME, "password", password)
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringError):
        save_baked_credentials(username, password)


def clear_credentials() -> None:
    for key in ("username", "password"):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass
        except (keyring.errors.NoKeyringError, keyring.errors.KeyringError):
            pass

    try:
        BAKED_CREDENTIALS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def prompt_credentials(warn_no_keyring: bool = False) -> tuple[str, str]:
    print("No stored credentials found. Please log in.\n")
    if warn_no_keyring:
        print("⚠  System keyring is unavailable. Credentials will be stored")
        print(f"   in a plain-text JSON file ({BAKED_CREDENTIALS_FILE}).")
        print("   Restrict access to that file or set up a keyring backend.\n")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    return email, password


def is_keyring_available() -> bool:
    """Return True if the system keyring is usable, False otherwise."""
    try:
        keyring.get_password(SERVICE_NAME, "__keyring_probe__")
        return True
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringError):
        return False


# ── API client ───────────────────────────────────────────────────────
class JNCApi:
    """Thin synchronous wrapper around the labs.j-novel.club v2 API."""

    def __init__(self, token: str | None = None):
        self.session = requests.Session()
        self.session.headers.update(_COMMON_HEADERS)
        self.session.params = {"format": "json"}  # type: ignore[assignment]
        self.token: str | None = token
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # ── auth ──
    def login(self, email: str, password: str) -> dict:
        """Authenticate and store the bearer token for future calls."""
        resp = self.session.post(
            f"{API_BASE}/auth/login",
            json={"login": email, "password": password, "slim": True},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["id"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        return data

    def logout(self) -> None:
        """Invalidate the current token server-side."""
        if self.token:
            self.session.post(f"{API_BASE}/auth/logout", timeout=10)
            self.token = None
            self.session.headers.pop("Authorization", None)

    # ── user ──
    def me(self) -> dict:
        resp = self.session.get(f"{API_BASE}/me", timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── library (purchased books, paginated) ──
    def library(self, limit: int = 100, skip: int = 0) -> dict:
        resp = self.session.get(
            f"{API_BASE}/me/library",
            params={"limit": limit, "skip": skip},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── public catalogue ──
    def series_list(self, limit: int = 25, skip: int = 0) -> dict:
        resp = self.session.get(
            f"{API_BASE}/series",
            params={"limit": limit, "skip": skip},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def series_detail(self, slug: str) -> dict:
        resp = self.session.get(
            f"{API_BASE}/series/{slug}/aggregate", timeout=15
        )
        resp.raise_for_status()
        return resp.json()


# ── library helpers ───────────────────────────────────────────────────
def parse_library(books: list[dict]) -> dict[str, dict]:
    """Group raw library books by series.

    Returns {series_id: {title, slug, books: [...]}} sorted by series title,
    with books sorted by volume number within each series.
    """
    series_map: dict[str, dict] = {}
    for b in books:
        serie = b.get("serie", {})
        sid = serie.get("id", "UNKNOWN")
        if sid not in series_map:
            series_map[sid] = {
                "title": serie.get("title", "Unknown Series"),
                "slug": serie.get("slug", ""),
                "books": [],
            }
        vol = b.get("volume", {})
        dl = b.get("downloads", [])
        epub_link = next((d["link"] for d in dl if d.get("type") == "EPUB"), None)
        series_map[sid]["books"].append({
            "id": b["id"],
            "title": vol.get("title", "?"),
            "short": vol.get("shortTitle", ""),
            "number": vol.get("number", 0),
            "slug": vol.get("slug", ""),
            "status": b.get("status", "?"),
            "epub_link": epub_link,
        })

    for info in series_map.values():
        info["books"].sort(key=lambda x: x["number"])

    return dict(sorted(series_map.items(), key=lambda kv: kv[1]["title"]))


def download_epub(epub_link: str, dest_path: str) -> int:
    """Download an EPUB from *epub_link* to *dest_path*.  Returns bytes written."""
    resp = requests.get(epub_link, timeout=120, stream=True)
    resp.raise_for_status()
    total = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            total += len(chunk)
    return total
