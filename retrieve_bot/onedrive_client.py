"""OneDrive integration via Microsoft Graph API.

All operations are confined to the configured target folder
(default: 'Project Retrieve'). No files or folders are ever deleted.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import msal
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv("ONEDRIVE_CLIENT_ID")
CLIENT_SECRET = os.getenv("ONEDRIVE_CLIENT_SECRET")
TENANT_ID = os.getenv("ONEDRIVE_TENANT_ID", "common")
TARGET_FOLDER = os.getenv("ONEDRIVE_TARGET_FOLDER", "Project Retrieve")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["Files.ReadWrite"]
TOKEN_CACHE_PATH = Path(__file__).parent.parent / "data" / "token_cache.json"


class OneDriveClient:
    """Handles OAuth2 auth and file operations scoped to TARGET_FOLDER."""

    def __init__(self):
        self.token: Optional[str] = None
        self.headers: dict = {}
        self._cache = msal.SerializableTokenCache()
        self._load_cache()

        self.app = msal.ConfidentialClientApplication(
            CLIENT_ID,
            authority=AUTHORITY,
            client_credential=CLIENT_SECRET,
            token_cache=self._cache,
        )

    @property
    def _base_url(self) -> str:
        return (
            f"https://graph.microsoft.com/v1.0/me/drive/root:/{TARGET_FOLDER}"
        )

    # ---- token cache persistence ----

    def _load_cache(self):
        if TOKEN_CACHE_PATH.exists():
            self._cache.deserialize(
                TOKEN_CACHE_PATH.read_text(encoding="utf-8")
            )

    def _save_cache(self):
        if self._cache.has_state_changed:
            TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE_PATH.write_text(
                self._cache.serialize(), encoding="utf-8"
            )

    def _set_token(self, token: str):
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}
        self._save_cache()

    # ---- authentication ----

    def authenticate_silent(self) -> bool:
        """Attempt to acquire a token from the cache / refresh token."""
        accounts = self.app.get_accounts()
        if not accounts:
            return False
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._set_token(result["access_token"])
            return True
        return False

    def get_device_flow(self) -> dict:
        """Start the OAuth2 device-code flow (returns the flow dict)."""
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow initiation failed: {flow}")
        return flow

    def complete_device_flow(self, flow: dict) -> bool:
        """Block until the user completes device-code authentication."""
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self._set_token(result["access_token"])
            return True
        logger.error(
            "Device flow failed: %s", result.get("error_description")
        )
        return False

    def is_authenticated(self) -> bool:
        if self.token:
            return True
        return self.authenticate_silent()

    def _ensure_auth(self):
        if not self.is_authenticated():
            raise RuntimeError(
                "OneDrive not authenticated. Use /auth_onedrive in Telegram."
            )

    # ---- folder operations (create only – never delete) ----

    def ensure_folder(self, subfolder_path: str = ""):
        """Create the full folder hierarchy under TARGET_FOLDER."""
        self._ensure_auth()

        parts = [TARGET_FOLDER]
        if subfolder_path:
            parts.extend(p for p in subfolder_path.strip("/").split("/") if p)

        current_path = ""
        for part in parts:
            parent_url = (
                f"https://graph.microsoft.com/v1.0/me/drive/root:"
                f"/{current_path}:/children"
                if current_path
                else "https://graph.microsoft.com/v1.0/me/drive/root/children"
            )
            body = {
                "name": part,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            }
            resp = requests.post(parent_url, headers=self.headers, json=body)
            if resp.status_code not in (201, 409):
                logger.warning(
                    "Folder creation issue for '%s': %s", part, resp.text
                )
            current_path = (
                f"{current_path}/{part}" if current_path else part
            )

    # ---- file operations (upload only – never delete) ----

    def upload_file(self, remote_path: str, content: bytes) -> bool:
        """Upload *content* to <TARGET_FOLDER>/<remote_path>."""
        self._ensure_auth()
        url = f"{self._base_url}/{remote_path}:/content"
        resp = requests.put(url, headers=self.headers, data=content)
        if resp.status_code in (200, 201):
            logger.info("Uploaded %s/%s", TARGET_FOLDER, remote_path)
            return True
        logger.error(
            "Upload failed (%s): %s", resp.status_code, resp.text
        )
        return False

    def list_files(self, subfolder: str = "") -> list:
        """List items inside a subfolder of TARGET_FOLDER."""
        self._ensure_auth()
        path = (
            f"{TARGET_FOLDER}/{subfolder}" if subfolder else TARGET_FOLDER
        )
        url = (
            f"https://graph.microsoft.com/v1.0/me/drive/root:/{path}:/children"
        )
        resp = requests.get(url, headers=self.headers)
        if resp.status_code == 200:
            return resp.json().get("value", [])
        return []
