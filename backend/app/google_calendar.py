from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .rentals.store import DocumentStore

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
OAUTH_STATE_TTL_SECONDS = 600
INTEGRATIONS_COLLECTION_SUFFIX = "/integrations"


class GoogleCalendarError(RuntimeError):
    pass


class InvalidGoogleCalendarState(GoogleCalendarError):
    pass


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encryption_key() -> bytes:
    encoded = os.getenv("GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY", "").strip()
    if not encoded:
        raise GoogleCalendarError("Google Calendar token encryption is not configured.")
    try:
        key = _b64url_decode(encoded)
    except (ValueError, base64.binascii.Error) as error:
        raise GoogleCalendarError("The Google Calendar token encryption key is invalid.") from error
    if len(key) != 32:
        raise GoogleCalendarError("The Google Calendar token encryption key must decode to 32 bytes.")
    return key


def _oauth_settings() -> tuple[str, str, str]:
    client_id = os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise GoogleCalendarError("Google Calendar OAuth is not configured on the server.")
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise GoogleCalendarError("The Google Calendar OAuth redirect URI is invalid.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise GoogleCalendarError("The Google Calendar OAuth redirect URI must use HTTPS outside local development.")
    _encryption_key()
    return client_id, client_secret, redirect_uri


def google_calendar_configured() -> bool:
    try:
        _oauth_settings()
    except GoogleCalendarError:
        return False
    return True


def _integration_collection(uid: str) -> str:
    return f"users/{uid}{INTEGRATIONS_COLLECTION_SUFFIX}"


def _integration(store: DocumentStore, uid: str) -> dict[str, Any] | None:
    return store.get(_integration_collection(uid), "googleCalendar")


def google_calendar_status(store: DocumentStore, uid: str) -> dict[str, bool]:
    integration = _integration(store, uid)
    return {
        "configured": google_calendar_configured(),
        "connected": bool(integration and integration.get("connected") is True),
    }


def frontend_result_url(result: str) -> str:
    frontend = os.getenv("SMARTWATTS_FRONTEND_URL", "").strip()
    if not frontend:
        frontend = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",", maxsplit=1)[0].strip()
    parsed = urlparse(frontend)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise GoogleCalendarError("The SmartWatts frontend URL is invalid.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise GoogleCalendarError("The SmartWatts frontend URL must use HTTPS outside local development.")
    return f"{frontend.rstrip('/')}/?{urlencode({'googleCalendar': result})}"


def _state_signing_key(encryption_key: bytes) -> bytes:
    return hmac.new(encryption_key, b"smartwatts-google-calendar-oauth-state", hashlib.sha256).digest()


def _encode_state(uid: str, nonce: str, expires_at: int, encryption_key: bytes) -> str:
    payload = _b64url_encode(json.dumps(
        {"uid": uid, "nonce": nonce, "exp": expires_at},
        separators=(",", ":"),
    ).encode("utf-8"))
    signature = hmac.new(_state_signing_key(encryption_key), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url_encode(signature)}"


def _decode_state(state: str) -> dict[str, Any]:
    try:
        payload, signature = state.split(".", maxsplit=1)
        key = _encryption_key()
        expected = hmac.new(_state_signing_key(key), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature)):
            raise InvalidGoogleCalendarState("The Google Calendar authorization state is invalid.")
        data = json.loads(_b64url_decode(payload))
    except InvalidGoogleCalendarState:
        raise
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error) as error:
        raise InvalidGoogleCalendarState("The Google Calendar authorization state is invalid.") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("uid"), str)
        or not isinstance(data.get("nonce"), str)
        or not isinstance(data.get("exp"), int)
        or data["exp"] < int(time.time())
    ):
        raise InvalidGoogleCalendarState("The Google Calendar authorization state has expired.")
    return data


def create_authorization_url(uid: str, store: DocumentStore) -> str:
    client_id, _, redirect_uri = _oauth_settings()
    nonce = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + OAUTH_STATE_TTL_SECONDS
    state_collection = _integration_collection(uid)
    state_record = {"used": False, "nonce": nonce, "expiresAt": expires_at}
    if store.get(state_collection, "googleCalendarOAuthState"):
        store.update(state_collection, "googleCalendarOAuthState", state_record)
    else:
        store.create(state_collection, "googleCalendarOAuthState", state_record)
    state = _encode_state(uid, nonce, expires_at, _encryption_key())
    query = urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': CALENDAR_SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
    })
    return f"{AUTHORIZATION_URL}?{query}"


def consume_authorization_state(state: str, store: DocumentStore) -> str:
    data = _decode_state(state)
    collection = _integration_collection(data["uid"])
    state_record = store.get(collection, "googleCalendarOAuthState")
    if (
        not state_record
        or state_record.get("nonce") != data["nonce"]
        or state_record.get("used") is not False
        or not isinstance(state_record.get("expiresAt"), int)
        or state_record["expiresAt"] < int(time.time())
    ):
        raise InvalidGoogleCalendarState("The Google Calendar authorization state has already been used or expired.")
    if not store.compare_and_update(
        collection,
        "googleCalendarOAuthState",
        {"used": False, "nonce": data["nonce"]},
        {"used": True},
    ):
        raise InvalidGoogleCalendarState("The Google Calendar authorization state has already been used.")
    return data["uid"]


def _encrypt_refresh_token(refresh_token: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, refresh_token.encode("utf-8"), b"smartwatts-google-calendar")
    return _b64url_encode(nonce + ciphertext)


def _decrypt_refresh_token(encrypted: str) -> str:
    try:
        payload = _b64url_decode(encrypted)
        token = AESGCM(_encryption_key()).decrypt(payload[:12], payload[12:], b"smartwatts-google-calendar")
        return token.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError, base64.binascii.Error) as error:
        raise GoogleCalendarError("The stored Google Calendar connection cannot be decrypted.") from error


async def complete_authorization(uid: str, code: str, store: DocumentStore) -> None:
    client_id, client_secret, redirect_uri = _oauth_settings()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(TOKEN_URL, data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
    except httpx.HTTPError as error:
        raise GoogleCalendarError("Could not exchange the Google Calendar authorization code.") from error
    if response.is_error:
        raise GoogleCalendarError(f"Google Calendar authorization failed (HTTP {response.status_code}).")
    try:
        token_data = response.json()
    except ValueError as error:
        raise GoogleCalendarError("Google returned an invalid authorization response.") from error
    if not isinstance(token_data, dict):
        raise GoogleCalendarError("Google returned an invalid authorization response.")

    refresh_token = token_data.get("refresh_token")
    existing = _integration(store, uid)
    if not isinstance(refresh_token, str) or not refresh_token:
        encrypted_existing = existing.get("encryptedRefreshToken") if existing else None
        if not isinstance(encrypted_existing, str) or not encrypted_existing:
            raise GoogleCalendarError("Google did not provide a refresh token. Please try connecting again.")
        encrypted_token = encrypted_existing
    else:
        encrypted_token = _encrypt_refresh_token(refresh_token)

    integration_data = {
        "connected": True,
        "encryptedRefreshToken": encrypted_token,
        "connectedAt": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        store.update(_integration_collection(uid), "googleCalendar", integration_data)
    else:
        store.create(_integration_collection(uid), "googleCalendar", integration_data)


async def _access_token(uid: str, store: DocumentStore) -> str:
    client_id, client_secret, _ = _oauth_settings()
    integration = _integration(store, uid)
    encrypted_refresh_token = integration.get("encryptedRefreshToken") if integration else None
    if not integration or integration.get("connected") is not True or not isinstance(encrypted_refresh_token, str):
        raise GoogleCalendarError("Google Calendar is not connected.")
    refresh_token = _decrypt_refresh_token(encrypted_refresh_token)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
    except httpx.HTTPError as error:
        raise GoogleCalendarError("Could not refresh Google Calendar authorization.") from error
    if response.is_error:
        raise GoogleCalendarError(f"Google Calendar authorization refresh failed (HTTP {response.status_code}).")
    try:
        token_data = response.json()
    except ValueError as error:
        raise GoogleCalendarError("Google returned an invalid token refresh response.") from error
    if not isinstance(token_data, dict):
        raise GoogleCalendarError("Google returned an invalid token refresh response.")
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleCalendarError("Google did not return an access token.")
    return access_token


def _event_id(uid: str, notice_id: str) -> str:
    return hashlib.sha256(f"{uid}:{notice_id}".encode("utf-8")).hexdigest()


async def upsert_interruption_event(
    uid: str,
    notice_id: str,
    notice: dict[str, Any],
    store: DocumentStore,
    access_token: str | None = None,
) -> tuple[str, str | None]:
    if not notice.get("startAt") or not notice.get("endAt"):
        return "not_scheduled", None
    if not google_calendar_configured():
        return "not_configured", None
    if not google_calendar_status(store, uid)["connected"]:
        return "not_connected", None
    bearer = access_token or await _access_token(uid, store)
    google_event_id = _event_id(uid, notice_id)
    event_url = f"{CALENDAR_API_URL}/calendars/primary/events/{google_event_id}"
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    event = {
        "summary": f"Power interruption: {notice.get('subject', 'MERALCO notice')}"[:1024],
        "description": str(notice.get("summary", ""))[:8192],
        "start": {"dateTime": notice["startAt"]},
        "end": {"dateTime": notice["endAt"]},
        "location": str(notice.get("serviceArea") or "")[:1024],
        "visibility": "private",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            existing = await client.get(event_url, headers=headers)
            if existing.status_code == 404:
                response = await client.post(
                    f"{CALENDAR_API_URL}/calendars/primary/events",
                    headers=headers,
                    json={"id": google_event_id, **event},
                )
                if response.status_code == 409:
                    response = await client.patch(event_url, headers=headers, json=event)
            elif existing.is_error:
                raise GoogleCalendarError(f"Google Calendar event lookup failed (HTTP {existing.status_code}).")
            else:
                response = await client.patch(event_url, headers=headers, json=event)
    except httpx.HTTPError as error:
        raise GoogleCalendarError("Could not sync an interruption schedule to Google Calendar.") from error
    if response.is_error:
        raise GoogleCalendarError(f"Google Calendar event sync failed (HTTP {response.status_code}).")
    return "synced", google_event_id


def _save_notice_sync(
    store: DocumentStore,
    uid: str,
    notice_id: str,
    status: str,
    event_id: str | None = None,
) -> None:
    changes: dict[str, Any] = {
        "calendarSync": status,
        "calendarSyncAt": datetime.now(timezone.utc).isoformat(),
    }
    if event_id:
        changes["calendarEventId"] = event_id
    store.update(f"users/{uid}/utilityNotices", notice_id, changes)


async def sync_existing_interruption_notices(uid: str, store: DocumentStore) -> dict[str, int]:
    if not google_calendar_configured():
        raise GoogleCalendarError("Google Calendar OAuth is not configured on the server.")
    if not google_calendar_status(store, uid)["connected"]:
        raise GoogleCalendarError("Connect Google Calendar before syncing interruption schedules.")
    access_token = await _access_token(uid, store)
    notices = store.query(f"users/{uid}/utilityNotices", kind="interruption")
    synced = skipped = failed = 0
    for notice in notices:
        notice_id = notice["id"]
        if not notice.get("startAt") or not notice.get("endAt"):
            _save_notice_sync(store, uid, notice_id, "not_scheduled")
            skipped += 1
            continue
        try:
            status, event_id = await upsert_interruption_event(
                uid, notice_id, notice, store, access_token=access_token,
            )
        except GoogleCalendarError:
            _save_notice_sync(store, uid, notice_id, "failed")
            failed += 1
            continue
        _save_notice_sync(store, uid, notice_id, status, event_id)
        if status == "synced":
            synced += 1
        else:
            skipped += 1
    return {"synced": synced, "skipped": skipped, "failed": failed}


async def disconnect_google_calendar(uid: str, store: DocumentStore) -> None:
    integration = _integration(store, uid)
    encrypted_refresh_token = integration.get("encryptedRefreshToken") if integration else None
    if not integration or integration.get("connected") is not True or not isinstance(encrypted_refresh_token, str):
        return
    refresh_token = _decrypt_refresh_token(encrypted_refresh_token)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(REVOKE_URL, data={"token": refresh_token})
    except httpx.HTTPError as error:
        raise GoogleCalendarError("Could not disconnect Google Calendar.") from error
    if response.is_error and response.status_code != 400:
        raise GoogleCalendarError(f"Google Calendar disconnection failed (HTTP {response.status_code}).")
    store.update(_integration_collection(uid), "googleCalendar", {
        "connected": False,
        "encryptedRefreshToken": "",
        "disconnectedAt": datetime.now(timezone.utc).isoformat(),
    })
