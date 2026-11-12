import json
import hashlib
import html
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
from firebase_admin import auth as firebase_auth
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin.exceptions import FirebaseError
from google.auth.exceptions import DefaultCredentialsError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

load_dotenv()

from .auth import AuthUser, get_firebase_app, require_user  # noqa: E402
from .advisor import AdvisorContext, compute_facts, deterministic_summary, system_prompt, unverified_figures  # noqa: E402
from .google_calendar import (  # noqa: E402
    GoogleCalendarError,
    complete_authorization,
    consume_authorization_state,
    create_authorization_url,
    disconnect_google_calendar,
    frontend_result_url,
    google_calendar_status,
    sync_existing_interruption_notices,
    upsert_interruption_event,
)
from .rentals.router import router as rentals_router  # noqa: E402
from .rentals.store import AlreadyExistsError, firestore_store_from_env  # noqa: E402

logger = logging.getLogger(__name__)
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
MAX_MESSAGES = 12
MAX_TRANSLATION_STRINGS = 80
MAX_TRANSLATION_CHARACTERS = 25000
LanguageCode = Annotated[str, Field(pattern=r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")]


class ChatMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    language: LanguageCode
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    # The signed-in user's own appliance/bill figures (optional). See app/advisor.py.
    context: AdvisorContext | None = None

    @model_validator(mode="after")
    def require_latest_user_message(self):
        if self.messages[-1].role != "user":
            raise ValueError("The latest chat message must be from the user.")
        return self


class ChatReply(BaseModel):
    reply: str
    # True when every peso/kWh/% figure in the reply matched a SmartWatts-computed fact.
    validated: bool = True
    facts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    withheldFigures: list[str] = Field(default_factory=list)


class ApplianceVisionRequest(BaseModel):
    image_base64: str = Field(min_length=10, max_length=15_000_000)
    mime_type: str = Field(default="image/jpeg")

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        clean = value.lower().strip()
        if clean not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
            raise ValueError("Supported image formats are JPEG, PNG, and WebP.")
        return "image/jpeg" if clean == "image/jpg" else clean


class ApplianceVisionResponse(BaseModel):
    brand: str | None = None
    model: str | None = None
    appliance_type: str | None = None
    watts: float | None = None
    voltage: str | None = None
    frequency: str | None = None
    current: str | None = None
    tested_kwh_month: float | None = None
    tested_kwh_24h: float | None = None
    tested_kwh_year: float | None = None
    energy_efficiency_rating: float | None = None
    label_type: str = "UNKNOWN"
    raw_text: str | None = None
    evidence: str | None = None
    confidence: str = "medium"
    notes: str | None = None


class TranslationRequest(BaseModel):
    target: LanguageCode
    texts: list[str] = Field(min_length=1, max_length=MAX_TRANSLATION_STRINGS)

    @model_validator(mode="after")
    def validate_texts(self):
        if any(not text.strip() or len(text) > 5000 for text in self.texts):
            raise ValueError("Translation text must contain 1 to 5,000 characters.")
        if sum(map(len, self.texts)) > MAX_TRANSLATION_CHARACTERS:
            raise ValueError("A translation request may contain at most 25,000 characters.")
        return self


class TranslationResponse(BaseModel):
    translations: list[str]


class MeralcoEmailNotice(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message_id: str = Field(min_length=1, max_length=500)
    account_email: str = Field(min_length=3, max_length=254)
    sender: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=20_000)
    received_at: datetime | None = None
    service_area: str | None = Field(default=None, max_length=160)
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("account_email")
    @classmethod
    def validate_account_email(cls, value: str) -> str:
        clean = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean):
            raise ValueError("A valid SmartWatts account email is required.")
        return clean

    @field_validator("sender")
    @classmethod
    def validate_meralco_sender(cls, value: str) -> str:
        clean = value.strip().lower()
        address = clean.rsplit("<", 1)[-1].rstrip(">").strip()
        domain = address.rsplit("@", 1)[1] if "@" in address else ""
        if domain != "meralco.com.ph" and not domain.endswith(".meralco.com.ph"):
            raise ValueError("Only forwarded emails from a meralco.com.ph address are accepted.")
        return value.strip()[:254]

    @model_validator(mode="after")
    def validate_event_times(self):
        if any(value is not None and value.tzinfo is None for value in (self.start_at, self.end_at)):
            raise ValueError("Interruption times must include a timezone offset.")
        if self.start_at and self.end_at:
            if self.end_at <= self.start_at:
                raise ValueError("The interruption end time must be after its start time.")
        return self


app = FastAPI(title="SmartWatts API", version="2.0.0")
allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(rentals_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


def _utility_notice_store():
    store = firestore_store_from_env()
    if store is None:
        raise HTTPException(status_code=503, detail="Utility notice storage is not configured on the server.")
    return store


def _email_notice_kind(subject: str, text: str) -> str:
    content = f"{subject} {text}".lower()
    if re.search(r"\b(interruption|power outage|brownout|scheduled maintenance)\b", content):
        return "interruption"
    if re.search(r"\b(disconnection|disconnect notice|service cut[- ]?off)\b", content):
        return "disconnection"
    return "report"


def _email_summary(text: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", text))
    return re.sub(r"\s+", " ", plain).strip()[:500]


@app.post("/api/inbound/meralco-email", status_code=202)
async def receive_meralco_email(
    notice: MeralcoEmailNotice,
    webhook_secret: Annotated[str | None, Header(alias="X-Meralco-Webhook-Secret")] = None,
):
    configured_secret = os.getenv("MERALCO_EMAIL_WEBHOOK_SECRET", "").strip()
    if not configured_secret:
        raise HTTPException(status_code=503, detail="The MERALCO inbound-email webhook is not configured.")
    if not webhook_secret or not hmac.compare_digest(webhook_secret, configured_secret):
        raise HTTPException(status_code=401, detail="The inbound-email webhook secret is invalid.")

    try:
        user = firebase_auth.get_user_by_email(notice.account_email, app=get_firebase_app())
    except firebase_auth.UserNotFoundError as error:
        raise HTTPException(status_code=404, detail="No SmartWatts account matches the forwarded email recipient.") from error
    except (DefaultCredentialsError, FirebaseError, ValueError) as error:
        raise HTTPException(status_code=503, detail="Could not verify the forwarded email recipient.") from error
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="The forwarded email recipient must be a verified SmartWatts account.")

    received_at = notice.received_at or datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    document_id = hashlib.sha256(notice.message_id.encode("utf-8")).hexdigest()
    document = {
        "kind": _email_notice_kind(notice.subject, notice.text),
        "subject": notice.subject.strip(),
        "summary": _email_summary(notice.text),
        "sender": notice.sender,
        "receivedAt": received_at.astimezone(timezone.utc).isoformat(),
        "serviceArea": notice.service_area.strip() if notice.service_area else None,
        "startAt": notice.start_at.isoformat() if notice.start_at else None,
        "endAt": notice.end_at.isoformat() if notice.end_at else None,
    }
    store = _utility_notice_store()
    try:
        store.create(f"users/{user.uid}/utilityNotices", document_id, document)
        duplicate = False
    except AlreadyExistsError:
        duplicate = True
        document = store.get(f"users/{user.uid}/utilityNotices", document_id) or document
    if document.get("kind") == "interruption":
        sync_status = "not_scheduled"
        calendar_event_id = None
        try:
            sync_status, calendar_event_id = await upsert_interruption_event(
                user.uid, document_id, document, store,
            )
        except GoogleCalendarError:
            sync_status = "failed"
            logger.exception("Could not sync MERALCO interruption notice %s to Google Calendar.", document_id)
        sync_update = {
            "calendarSync": sync_status,
            "calendarSyncAt": datetime.now(timezone.utc).isoformat(),
        }
        if calendar_event_id:
            sync_update["calendarEventId"] = calendar_event_id
        store.update(f"users/{user.uid}/utilityNotices", document_id, sync_update)
    return {"accepted": True, "duplicate": duplicate}


@app.get("/api/utility-notices")
def list_utility_notices(user: AuthUser = Depends(require_user)):
    if user.is_anonymous or not user.email_verified:
        raise HTTPException(status_code=403, detail="A verified email account is required to view private utility notices.")
    notices = _utility_notice_store().query(f"users/{user.uid}/utilityNotices")
    notices.sort(key=lambda item: item.get("receivedAt", ""), reverse=True)
    return notices


def require_verified_calendar_user(user: AuthUser = Depends(require_user)) -> AuthUser:
    if user.is_anonymous or not user.email_verified:
        raise HTTPException(status_code=403, detail="A verified email account is required to connect Google Calendar.")
    return user


@app.get("/api/google-calendar/status")
def get_google_calendar_status(user: AuthUser = Depends(require_verified_calendar_user)):
    return google_calendar_status(_utility_notice_store(), user.uid)


@app.post("/api/google-calendar/authorize")
def authorize_google_calendar(user: AuthUser = Depends(require_verified_calendar_user)):
    try:
        return {"authorizationUrl": create_authorization_url(user.uid, _utility_notice_store())}
    except GoogleCalendarError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/google-calendar/callback", include_in_schema=False)
async def google_calendar_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    result = "error"
    if state:
        try:
            store = _utility_notice_store()
            uid = consume_authorization_state(state, store)
            if error:
                result = "cancelled"
            elif code:
                await complete_authorization(uid, code, store)
                result = "connected"
                try:
                    await sync_existing_interruption_notices(uid, store)
                except GoogleCalendarError:
                    logger.exception("Google Calendar connected, but existing interruption schedules could not be synced.")
        except GoogleCalendarError:
            logger.exception("Google Calendar authorization callback failed.")
    return RedirectResponse(frontend_result_url(result), status_code=303)


@app.post("/api/google-calendar/sync")
async def sync_google_calendar(user: AuthUser = Depends(require_verified_calendar_user)):
    try:
        return await sync_existing_interruption_notices(user.uid, _utility_notice_store())
    except GoogleCalendarError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/google-calendar/disconnect")
async def unlink_google_calendar(user: AuthUser = Depends(require_verified_calendar_user)):
    try:
        await disconnect_google_calendar(user.uid, _utility_notice_store())
    except GoogleCalendarError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {"connected": False}


async def google_translation_request(method: str, *, params: dict[str, str], payload: dict | None = None):
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Google Cloud Translation is not configured on the server.")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.request(
                method,
                GOOGLE_TRANSLATE_URL,
                params={**params, "key": api_key},
                json=payload,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="Google Cloud Translation timed out. Please try again.") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="Could not reach Google Cloud Translation.") from error

    if response.status_code in (401, 403):
        raise HTTPException(status_code=503, detail="Google Cloud Translation is unavailable for this server configuration.")
    if response.status_code == 400:
        raise HTTPException(status_code=400, detail="Google Cloud Translation rejected the language or text.")
    if response.is_error:
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an error.")

    try:
        result = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an invalid response.") from error
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an invalid response.")
    return result


@app.get("/api/translate/languages")
async def translation_languages(_user: AuthUser = Depends(require_user)):
    result = await google_translation_request("GET", params={"target": "en"})
    data = result.get("data")
    languages = data.get("languages") if isinstance(data, dict) else None
    if not isinstance(languages, list):
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an invalid language list.")

    supported = [
        {"code": language["language"], "name": language["name"]}
        for language in languages
        if isinstance(language, dict)
        and isinstance(language.get("language"), str)
        and isinstance(language.get("name"), str)
    ]
    if not supported:
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an empty language list.")
    return {"languages": supported}


@app.post("/api/translate", response_model=TranslationResponse)
async def translate(request: TranslationRequest, _user: AuthUser = Depends(require_user)):
    result = await google_translation_request(
        "POST",
        params={},
        payload={"q": request.texts, "target": request.target, "format": "text"},
    )
    data = result.get("data")
    translated = data.get("translations") if isinstance(data, dict) else None
    if not isinstance(translated, list) or len(translated) != len(request.texts):
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an incomplete translation.")

    translations = [item.get("translatedText") for item in translated if isinstance(item, dict)]
    if len(translations) != len(request.texts) or not all(isinstance(text, str) for text in translations):
        raise HTTPException(status_code=502, detail="Google Cloud Translation returned an invalid translation.")
    return TranslationResponse(translations=translations)


@app.post("/api/chat", response_model=ChatReply)
async def chat(request: ChatRequest, _user: AuthUser = Depends(require_user)):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="The AI service is not configured on the server.")

    contents = [
        {
            "role": "model" if message.role == "assistant" else "user",
            "parts": [{"text": message.text}],
        }
        for message in request.messages
    ]
    facts, assumptions = compute_facts(request.context)
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt(request.language, facts, assumptions)}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
    }

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(
                GEMINI_API_URL.format(model=model),
                headers={"x-goog-api-key": api_key},
                json=payload,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="The AI service timed out. Please try again.") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="Could not reach the AI service. Please try again.") from error

    if response.is_error:
        raise HTTPException(status_code=502, detail="The AI service returned an error. Please try again later.")

    try:
        result = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="The AI service returned an invalid response.") from error
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="The AI service returned an invalid response.")
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise HTTPException(status_code=502, detail="The AI service returned an empty reply.")
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise HTTPException(status_code=502, detail="The AI service returned an empty reply.")
    reply = next(
        (part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)),
        None,
    )
    if not reply or not reply.strip():
        raise HTTPException(status_code=502, detail="The AI service returned an empty reply.")
    fact_texts = [fact.text for fact in facts]
    user_text = " ".join(message.text for message in request.messages if message.role == "user")
    withheld = unverified_figures(reply, facts, user_text)
    if withheld:
        # Never pass invented figures to the user: replace with the deterministic calculation.
        return ChatReply(reply=deterministic_summary(facts, assumptions), validated=False,
                         facts=fact_texts, assumptions=assumptions, withheldFigures=withheld)
    return ChatReply(reply=reply.strip(), facts=fact_texts, assumptions=assumptions)


@app.post("/api/ai/appliance-label", response_model=ApplianceVisionResponse)
async def analyze_appliance_label(request: ApplianceVisionRequest, _user: AuthUser = Depends(require_user)):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="The AI vision service is not configured on the server. Please use local browser OCR or enter specifications manually.",
        )

    # Clean base64 string if data URL prefix was provided
    b64_data = request.image_base64
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    prompt_text = (
        "Analyze this appliance specification rating label or energy efficiency guide image.\n"
        "Extract only what is visibly printed on the label. Never invent missing specifications.\n"
        "Return a valid JSON object with the following fields:\n"
        "- label_type: 'ELECTRICAL_SPECIFICATION' | 'ENERGY_EFFICIENCY' | 'MIXED' | 'UNKNOWN'\n"
        "- brand: string or null (brand name, only if printed)\n"
        "- model: string or null (model number/code)\n"
        "- appliance_type: string or null (e.g. 'Air Conditioner', 'Refrigerator', 'Washing Machine', 'Fan', 'Microwave')\n"
        "- watts: number in watts (e.g. 65 for 65W, 500 for 0.5kW) or null. Must be electrical input wattage, NOT cooling capacity BTU/h and NOT motor horsepower HP.\n"
        "- voltage: string or null (e.g. '220V', '230V')\n"
        "- frequency: string or null (e.g. '60Hz')\n"
        "- current: string or null (e.g. '1.2A')\n"
        "- tested_kwh_month: number or null (tested monthly consumption in kWh/month if present on energy guide label)\n"
        "- tested_kwh_24h: number or null (tested daily consumption in kWh/24h if printed)\n"
        "- tested_kwh_year: number or null (tested annual consumption in kWh/year if printed)\n"
        "- energy_efficiency_rating: number or null (Energy Efficiency Rating / EER / Star rating e.g. 253 or 4.5. NEVER confuse EER with wattage or kWh)\n"
        "- raw_text: string (concise transcript of visible text on label)\n"
        "- evidence: string (quote the exact text showing ratings or state why wattage is missing)\n"
        "- confidence: 'high' | 'medium' | 'low'\n"
        "- notes: string or null (any relevant notes, e.g. 'Official Energy Guide label with tested monthly kWh and EER, no rated input wattage')"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": request.mime_type,
                            "data": b64_data,
                        }
                    },
                    {"text": prompt_text},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 800,
            "responseMimeType": "application/json",
        },
    }

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(
                GEMINI_API_URL.format(model=model),
                headers={"x-goog-api-key": api_key},
                json=payload,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="The AI vision service timed out. Please try again.") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="Could not reach the AI vision service. Please try again.") from error

    if response.is_error:
        raise HTTPException(status_code=502, detail="The AI vision service returned an error. Please try again later.")

    try:
        result = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="The AI vision service returned an invalid response.") from error

    candidates = result.get("candidates") if isinstance(result, dict) else None
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise HTTPException(status_code=502, detail="The AI vision service returned an empty reply.")

    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise HTTPException(status_code=502, detail="The AI vision service returned an empty reply.")

    raw_json_str = next(
        (part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)),
        None,
    )
    if not raw_json_str or not raw_json_str.strip():
        raise HTTPException(status_code=502, detail="The AI vision service returned an empty reply.")

    # Parse JSON output from model
    try:
        data = json.loads(raw_json_str.strip())
    except json.JSONDecodeError:
        # Attempt to strip markdown code blocks if present
        clean_str = re.sub(r"^```(?:json)?\s*", "", raw_json_str.strip(), flags=re.MULTILINE)
        clean_str = re.sub(r"\s*```$", "", clean_str, flags=re.MULTILINE)
        try:
            data = json.loads(clean_str.strip())
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=502, detail="The AI vision response could not be parsed as structured data.") from error

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="The AI vision service did not return an object.")

    # Deterministic validation of extracted values
    raw_watts = data.get("watts")
    validated_watts: float | None = None
    if isinstance(raw_watts, (int, float)) and 1 <= raw_watts <= 20000:
        validated_watts = float(raw_watts)

    raw_tested_kwh = data.get("tested_kwh_month")
    validated_kwh: float | None = None
    if isinstance(raw_tested_kwh, (int, float)) and 0 < raw_tested_kwh < 5000:
        validated_kwh = float(raw_tested_kwh)

    raw_tested_24h = data.get("tested_kwh_24h")
    validated_24h: float | None = None
    if isinstance(raw_tested_24h, (int, float)) and 0 < raw_tested_24h < 200:
        validated_24h = float(raw_tested_24h)

    raw_tested_year = data.get("tested_kwh_year")
    validated_year: float | None = None
    if isinstance(raw_tested_year, (int, float)) and 0 < raw_tested_year < 60000:
        validated_year = float(raw_tested_year)

    raw_eer = data.get("energy_efficiency_rating")
    validated_eer: float | None = None
    if isinstance(raw_eer, (int, float)) and 0 < raw_eer < 2000:
        validated_eer = float(raw_eer)

    label_type = str(data.get("label_type", "UNKNOWN")).upper().strip()
    if label_type not in {"ELECTRICAL_SPECIFICATION", "ENERGY_EFFICIENCY", "MIXED"}:
        has_energy = validated_kwh is not None or validated_24h is not None or validated_year is not None or validated_eer is not None
        if validated_watts is not None and has_energy:
            label_type = "MIXED"
        elif has_energy:
            label_type = "ENERGY_EFFICIENCY"
        elif validated_watts is not None:
            label_type = "ELECTRICAL_SPECIFICATION"
        else:
            label_type = "UNKNOWN"

    confidence = str(data.get("confidence", "medium")).lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    return ApplianceVisionResponse(
        brand=str(data["brand"]).strip() if data.get("brand") else None,
        model=str(data["model"]).strip() if data.get("model") else None,
        appliance_type=str(data["appliance_type"]).strip() if data.get("appliance_type") else None,
        watts=validated_watts,
        voltage=str(data["voltage"]).strip() if data.get("voltage") else None,
        frequency=str(data["frequency"]).strip() if data.get("frequency") else None,
        current=str(data["current"]).strip() if data.get("current") else None,
        tested_kwh_month=validated_kwh,
        tested_kwh_24h=validated_24h,
        tested_kwh_year=validated_year,
        energy_efficiency_rating=validated_eer,
        label_type=label_type,
        raw_text=str(data["raw_text"]).strip() if data.get("raw_text") else None,
        evidence=str(data["evidence"]).strip() if data.get("evidence") else None,
        confidence=confidence,
        notes=str(data["notes"]).strip() if data.get("notes") else None,
    )
