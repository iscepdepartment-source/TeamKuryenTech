import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.auth import AuthUser
from app.google_calendar import CALENDAR_SCOPE, TOKEN_URL
from app.main import _email_notice_kind, app, require_user
from app.rentals.store import InMemoryStore

client = TestClient(app)


def allow_test_guest():
    return AuthUser(uid="test-guest", sign_in_provider="anonymous")


app.dependency_overrides[require_user] = allow_test_guest


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_inbound_meralco_email_becomes_a_private_idempotent_notice(monkeypatch):
    monkeypatch.setenv("MERALCO_EMAIL_WEBHOOK_SECRET", "test-webhook-secret")
    store = InMemoryStore()
    monkeypatch.setattr("app.main.firestore_store_from_env", lambda: store)
    monkeypatch.setattr("app.main.get_firebase_app", lambda: object())
    monkeypatch.setattr(
        "app.main.firebase_auth.get_user_by_email",
        lambda _email, app: SimpleNamespace(uid="verified-user", email_verified=True),
    )
    payload = {
        "message_id": "meralco-message-1",
        "account_email": "user@example.com",
        "sender": "alerts@meralco.com.ph",
        "subject": "Scheduled power interruption",
        "text": "Power interruption in Makati on October 12.",
        "service_area": "Makati",
        "start_at": "2026-10-12T09:00:00+08:00",
        "end_at": "2026-10-12T11:00:00+08:00",
    }

    received = client.post(
        "/api/inbound/meralco-email",
        headers={"X-Meralco-Webhook-Secret": "test-webhook-secret"},
        json=payload,
    )
    duplicate = client.post(
        "/api/inbound/meralco-email",
        headers={"X-Meralco-Webhook-Secret": "test-webhook-secret"},
        json=payload,
    )
    assert received.status_code == 202
    assert received.json() == {"accepted": True, "duplicate": False}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": True, "duplicate": True}

    app.dependency_overrides[require_user] = lambda: AuthUser(
        uid="verified-user", sign_in_provider="password", email="user@example.com", email_verified=True,
    )
    try:
        notices = client.get("/api/utility-notices")
    finally:
        app.dependency_overrides[require_user] = allow_test_guest
    assert notices.status_code == 200
    assert notices.json()[0]["kind"] == "interruption"
    assert notices.json()[0]["serviceArea"] == "Makati"
    assert "October 12" in notices.json()[0]["summary"]


def test_inbound_meralco_email_rejects_bad_secret_and_non_meralco_sender(monkeypatch):
    monkeypatch.setenv("MERALCO_EMAIL_WEBHOOK_SECRET", "test-webhook-secret")
    payload = {
        "message_id": "message",
        "account_email": "user@example.com",
        "sender": "alerts@meralco.com.ph",
        "subject": "Notice",
        "text": "A message",
    }
    unauthorized = client.post(
        "/api/inbound/meralco-email",
        headers={"X-Meralco-Webhook-Secret": "wrong-secret"},
        json=payload,
    )
    payload["sender"] = "alerts@example.com"
    invalid_sender = client.post(
        "/api/inbound/meralco-email",
        headers={"X-Meralco-Webhook-Secret": "test-webhook-secret"},
        json=payload,
    )

    assert unauthorized.status_code == 401
    assert invalid_sender.status_code == 422


def test_private_utility_notices_require_a_verified_permanent_account():
    response = client.get("/api/utility-notices")
    assert response.status_code == 403


def test_google_calendar_authorization_syncs_notices_and_stores_only_an_encrypted_refresh_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_URI", "http://localhost:8000/api/google-calendar/callback")
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg")
    monkeypatch.setenv("SMARTWATTS_FRONTEND_URL", "http://localhost:5173")
    store = InMemoryStore()
    monkeypatch.setattr("app.main.firestore_store_from_env", lambda: store)
    app.dependency_overrides[require_user] = lambda: AuthUser(
        uid="verified-user", sign_in_provider="password", email="user@example.com", email_verified=True,
    )
    store.create("users/verified-user/utilityNotices", "notice-1", {
        "kind": "interruption",
        "subject": "Scheduled power interruption",
        "summary": "Makati service area",
        "serviceArea": "Makati",
        "startAt": "2026-10-12T09:00:00+08:00",
        "endAt": "2026-10-12T11:00:00+08:00",
    })
    requests = {"event_ids": set(), "event": None}

    class MockResponse:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self.is_error = status_code >= 400
            self.data = data or {}

        def json(self):
            return self.data

    class MockAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, data=None, headers=None, json=None):
            if url == TOKEN_URL:
                if data["grant_type"] == "authorization_code":
                    return MockResponse(200, {
                        "access_token": "initial-access",
                        "refresh_token": "refresh-token-secret",
                    })
                return MockResponse(200, {"access_token": "calendar-access"})
            requests["event"] = json
            requests["event_ids"].add(json["id"])
            return MockResponse(200, {"id": json["id"]})

        async def get(self, url, *, headers=None):
            event_id = url.rsplit("/", maxsplit=1)[-1]
            return MockResponse(200 if event_id in requests["event_ids"] else 404)

        async def patch(self, _url, *, headers=None, json=None):
            requests["event"] = json
            return MockResponse(200)

    monkeypatch.setattr("app.google_calendar.httpx.AsyncClient", MockAsyncClient)
    try:
        authorization = client.post("/api/google-calendar/authorize")
        assert authorization.status_code == 200
        query = parse_qs(urlparse(authorization.json()["authorizationUrl"]).query)
        assert query["scope"] == [CALENDAR_SCOPE]
        assert query["access_type"] == ["offline"]
        callback = client.get(
            "/api/google-calendar/callback",
            params={"code": "auth-code", "state": query["state"][0]},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "http://localhost:5173/?googleCalendar=connected"
        assert len(requests["event_ids"]) == 1
        assert requests["event"]["start"]["dateTime"] == "2026-10-12T09:00:00+08:00"
        assert store.get("users/verified-user/utilityNotices", "notice-1")["calendarSync"] == "synced"

        integration = store.get("users/verified-user/integrations", "googleCalendar")
        assert integration["connected"] is True
        assert "refresh-token-secret" not in integration["encryptedRefreshToken"]
        assert client.get("/api/google-calendar/status").json() == {"configured": True, "connected": True}
        synced = client.post("/api/google-calendar/sync")
        assert synced.status_code == 200
        assert synced.json() == {"synced": 1, "skipped": 0, "failed": 0}
        assert requests["event"]["summary"] == "Power interruption: Scheduled power interruption"
        replayed = client.get(
            "/api/google-calendar/callback",
            params={"code": "auth-code", "state": query["state"][0]},
            follow_redirects=False,
        )
        assert replayed.status_code == 303
        assert replayed.headers["location"] == "http://localhost:5173/?googleCalendar=error"

        monkeypatch.setenv("MERALCO_EMAIL_WEBHOOK_SECRET", "test-webhook-secret")
        monkeypatch.setattr("app.main.get_firebase_app", lambda: object())
        monkeypatch.setattr(
            "app.main.firebase_auth.get_user_by_email",
            lambda _email, app: SimpleNamespace(uid="verified-user", email_verified=True),
        )
        incoming = client.post(
            "/api/inbound/meralco-email",
            headers={"X-Meralco-Webhook-Secret": "test-webhook-secret"},
            json={
                "message_id": "new-interruption",
                "account_email": "user@example.com",
                "sender": "alerts@meralco.com.ph",
                "subject": "New scheduled power interruption",
                "text": "Power interruption in Pasig.",
                "service_area": "Pasig",
                "start_at": "2026-10-13T10:00:00+08:00",
                "end_at": "2026-10-13T12:00:00+08:00",
            },
        )
        assert incoming.status_code == 202
        assert len(requests["event_ids"]) == 2
        new_notice_id = hashlib.sha256(b"new-interruption").hexdigest()
        assert store.get("users/verified-user/utilityNotices", new_notice_id)["calendarSync"] == "synced"
    finally:
        app.dependency_overrides[require_user] = allow_test_guest


def test_email_notice_categories_include_disconnection_and_report():
    assert _email_notice_kind("Disconnection notice", "Service cut-off scheduled.") == "disconnection"
    assert _email_notice_kind("Your account statement", "A billing report is attached.") == "report"


def test_chat_requires_a_server_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = client.post(
        "/api/chat",
        json={"language": "en", "messages": [{"role": "user", "text": "How can I save energy?"}]},
    )
    assert response.status_code == 503


def test_chat_requires_firebase_authentication():
    app.dependency_overrides.pop(require_user)
    try:
        response = client.post(
            "/api/chat",
            json={"language": "en", "messages": [{"role": "user", "text": "How can I save energy?"}]},
        )
    finally:
        app.dependency_overrides[require_user] = allow_test_guest
    assert response.status_code == 401


def test_chat_requires_latest_message_from_user():
    response = client.post(
        "/api/chat",
        json={"language": "en", "messages": [{"role": "assistant", "text": "Hello"}]},
    )
    assert response.status_code == 422


def test_chat_rejects_malformed_language_code():
    response = client.post(
        "/api/chat",
        json={"language": "not a language", "messages": [{"role": "user", "text": "Bonjour"}]},
    )
    assert response.status_code == 422


def test_translation_requires_a_server_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_TRANSLATE_API_KEY", raising=False)
    response = client.get("/api/translate/languages")
    assert response.status_code == 503


def test_translation_language_list_requires_authentication():
    app.dependency_overrides.pop(require_user)
    try:
        response = client.get("/api/translate/languages")
    finally:
        app.dependency_overrides[require_user] = allow_test_guest
    assert response.status_code == 401


def test_translation_languages_and_text_are_proxied_server_side(monkeypatch):
    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "server-only-test-key")

    class MockResponse:
        status_code = 200
        is_error = False

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class MockAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, _url, *, params, json=None):
            assert params["key"] == "server-only-test-key"
            if method == "GET":
                return MockResponse({"data": {"languages": [
                    {"language": "en", "name": "English"},
                    {"language": "es", "name": "Spanish"},
                ]}})
            assert json == {"q": ["Good morning", "Usage"], "target": "es", "format": "text"}
            return MockResponse({"data": {"translations": [
                {"translatedText": "Buenos días"},
                {"translatedText": "Uso"},
            ]}})

    monkeypatch.setattr("app.main.httpx.AsyncClient", MockAsyncClient)

    languages = client.get("/api/translate/languages")
    assert languages.status_code == 200
    assert languages.json() == {"languages": [
        {"code": "en", "name": "English"},
        {"code": "es", "name": "Spanish"},
    ]}

    translated = client.post(
        "/api/translate",
        json={"target": "es", "texts": ["Good morning", "Usage"]},
    )
    assert translated.status_code == 200
    assert translated.json() == {"translations": ["Buenos días", "Uso"]}


def test_translation_rejects_oversized_batches():
    response = client.post(
        "/api/translate",
        json={"target": "es", "texts": ["x" * 5000 for _ in range(6)]},
    )
    assert response.status_code == 422


def test_permanent_accounts_can_use_personal_ai_features(monkeypatch):
    """Anonymous-to-permanent upgrade keeps the uid; the upgraded token must still work."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app.dependency_overrides[require_user] = lambda: AuthUser(uid="test-guest", sign_in_provider="password", email="a@b.co")
    try:
        response = client.post("/api/chat", json={"language": "en", "messages": [{"role": "user", "text": "Hi"}]})
    finally:
        app.dependency_overrides[require_user] = allow_test_guest
    assert response.status_code == 503  # reached the handler (no Gemini key), not rejected by auth


def test_plans_endpoint_lists_exactly_two_tiers():
    response = client.get("/api/plans")
    assert response.status_code == 200
    body = response.json()
    assert [tier["id"] for tier in body["tiers"]] == ["personal", "rentals"]
    assert body["tiers"][0]["priceCentavos"] == 0
    assert body["tiers"][1]["priceCentavos"] == 29900
    assert body["tiers"][1]["unitLimit"] == 10
    assert body["paymentsEnabled"] is False


def test_ai_appliance_label_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = client.post(
        "/api/ai/appliance-label",
        json={"image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "mime_type": "image/png"},
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_ai_appliance_label_requires_auth():
    app.dependency_overrides.pop(require_user)
    try:
        response = client.post(
            "/api/ai/appliance-label",
            json={"image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "mime_type": "image/png"},
        )
    finally:
        app.dependency_overrides[require_user] = allow_test_guest
    assert response.status_code == 401


def test_ai_appliance_label_rejects_invalid_mime_type():
    response = client.post(
        "/api/ai/appliance-label",
        json={"image_base64": "dGVzdC1kYXRh", "mime_type": "application/pdf"},
    )
    assert response.status_code == 422


def test_ai_appliance_label_success(monkeypatch):
    import json
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

    mock_json = json.dumps({
        "brand": "Carrier",
        "model": "FP-53CAS010",
        "appliance_type": "Air Conditioner",
        "watts": 850,
        "voltage": "230V",
        "frequency": "60Hz",
        "current": "3.8A",
        "tested_kwh_month": None,
        "raw_text": "Carrier Inverter Air Conditioner FP-53CAS010 Rated Input 850W 230V 60Hz",
        "evidence": "Rated Input 850W",
        "confidence": "high",
        "notes": "Clear rating plate",
    })

    class MockAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            assert headers["x-goog-api-key"] == "test-gemini-key"
            assert "gemini" in url
            class MockResponse:
                status_code = 200
                is_error = False
                def json(self):
                    return {
                        "candidates": [
                            {"content": {"parts": [{"text": mock_json}]}}
                        ]
                    }
            return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    response = client.post(
        "/api/ai/appliance-label",
        json={
            "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "mime_type": "image/png",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["brand"] == "Carrier"
    assert data["model"] == "FP-53CAS010"
    assert data["appliance_type"] == "Air Conditioner"
    assert data["watts"] == 850.0
    assert data["voltage"] == "230V"
    assert data["confidence"] == "high"
    assert data["label_type"] == "ELECTRICAL_SPECIFICATION"


def test_ai_appliance_energy_efficiency_label_success(monkeypatch):
    import json
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

    mock_json = json.dumps({
        "label_type": "ENERGY_EFFICIENCY",
        "brand": "LG",
        "model": "GR-B202SQBB",
        "appliance_type": "Refrigerator",
        "watts": None,
        "voltage": "230V",
        "frequency": "60Hz",
        "current": None,
        "tested_kwh_month": 26.10,
        "tested_kwh_24h": 0.87,
        "tested_kwh_year": 313.2,
        "energy_efficiency_rating": 253.0,
        "raw_text": "ENERGY GUIDE Monthly Energy Consumption 26.10 kWh EER 253",
        "evidence": "Tested monthly energy consumption: 26.10 kWh. No electrical wattage printed on energy guide label.",
        "confidence": "high",
        "notes": "Energy Guide label with tested monthly consumption",
    })

    class MockAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            class MockResponse:
                status_code = 200
                is_error = False
                def json(self):
                    return {
                        "candidates": [
                            {"content": {"parts": [{"text": mock_json}]}}
                        ]
                    }
            return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    response = client.post(
        "/api/ai/appliance-label",
        json={
            "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "mime_type": "image/png",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["brand"] == "LG"
    assert data["model"] == "GR-B202SQBB"
    assert data["appliance_type"] == "Refrigerator"
    assert data["watts"] is None
    assert data["tested_kwh_month"] == 26.10
    assert data["tested_kwh_24h"] == 0.87
    assert data["tested_kwh_year"] == 313.2
    assert data["energy_efficiency_rating"] == 253.0
    assert data["label_type"] == "ENERGY_EFFICIENCY"


