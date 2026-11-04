"""LLM clients (Gemini, OpenRouter) and the message-interpretation prompt."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Callable, Protocol

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You turn casual chat messages from a family into calendar actions.
Reply with ONE JSON object only, no prose, no code fences.

Schema:
{
  "intent": "create" | "edit" | "delete" | "list" | "cancel" | "help" | "chat",
  "continues_pending": true | false,
  "events": [EventFields, ...],        // intent=create: every event in the message
  "target_event_id": string | null,    // intent=edit/delete: id from KNOWN EVENTS
  "changes": EventFields,              // intent=edit: ONLY the fields being changed
  "reply": string | null               // intent=chat only: one short friendly sentence
}
EventFields = {
  "title": string | null,       // short, capitalized, e.g. "Tennis", "Dentist – kids"
  "date": "YYYY-MM-DD" | null,  // start date
  "end_date": "YYYY-MM-DD" | null,  // only for multi-day events
  "start_time": "HH:MM" | null, // 24-hour, only if a clock time was said
  "end_time": "HH:MM" | null,
  "time_hint": "morning" | "noon" | "afternoon" | "evening" | "night" | null,
  "location": string | null,
  "notes": string | null        // extra details worth keeping (who, what to bring...)
}

Rules:
- Dates: ALWAYS copy dates from the DATE REFERENCE table; never compute them yourself.
  "next <weekday>" and "<weekday>"/"this <weekday>" have different rows - use the matching one.
- Times: fill start_time/end_time only when a clock time is said ("7", "7:30", "noon" counts as 12:00).
  Read hours sensibly from context: "tennis Saturday morning 7 to 9:30" -> 07:00-09:30;
  "dinner at 7" -> 19:00. If only a vague time ("morning", "evening", "tonight") is given,
  leave start_time null and set time_hint. If no time at all, leave both null (all-day event).
- Never invent a title, date, time or location that was not said or clearly implied.
- intent=create when the message describes something to put on the calendar.
- intent=edit for changes to an existing event ("move it to 8am", "change the title to X",
  "make the dentist 30 minutes later", "add location Plano"). "it"/"that" means LAST EVENT.
  Otherwise match by title/date in KNOWN EVENTS. For a time-only change, set only start_time
  (and end_time if said). For "X minutes later/earlier", compute the new start_time/end_time.
  To remove a location or notes, set that field to "".
- intent=delete for "delete/remove/cancel the <event>". A bare "cancel", "never mind",
  "forget it", "start over" with no event named is intent=cancel.
- intent=list for "what's coming up", "what do we have this week", etc.
- intent=help for questions about what the bot can do. intent=chat for anything else.
- PENDING: if present, the bot asked a clarifying question. If the new message answers it,
  set continues_pending=true and return the COMPLETE merged events (pending fields + answer).
  If the new message is a fresh, self-contained event or a different request, set
  continues_pending=false and ignore the pending draft.
- If target_event_id cannot be identified with confidence, set it to null.
"""


# Estimated USD per 1M tokens (input, output). Thinking tokens bill as output.
# Check ai.google.dev/pricing or your AI Studio billing page for the authoritative numbers.
PRICES_PER_M = {
    "gemini-3.8-flash": (0.825, 4.125),
    "gemini-3.5-flash": (1.50, 9.00),
}

UsageCallback = Callable[[str, int, int], None]  # (model, input_tokens, output_tokens)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_PER_M.get(model)
    if not price:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


Attachment = tuple[bytes, str]  # (file bytes, MIME type), e.g. a photo or PDF


class LLM(Protocol):
    async def interpret(self, context: str, message: str) -> dict: ...

    async def complete(self, system: str, text: str, attachment: Attachment | None = None,
                       max_tokens: int = 4096) -> dict: ...


class LLMError(RuntimeError):
    pass


class LLMBillingError(LLMError):
    """Out of credits / billing problem: retrying or switching models will not help."""


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


class OpenRouterLLM:
    def __init__(self, api_key: str, models: list[str], zdr: bool = True, timeout: float = 90.0):
        self.models = models
        self.zdr = zdr
        self.on_usage: UsageCallback | None = None
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/local/family-event-bot",
                "X-Title": "Family Event Bot",
            },
        )

    def _payload(self, context: str, message: str) -> dict:
        return self._body(SYSTEM_PROMPT, f"{context}\n\nMESSAGE:\n{message}", None, 800)

    def _body(self, system: str, text: str, attachment: Attachment | None, max_tokens: int) -> dict:
        provider: dict = {"data_collection": "deny", "allow_fallbacks": True}
        if self.zdr:
            provider["zdr"] = True
        content: str | list = text
        if attachment:
            data, mime = attachment
            uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            part = ({"type": "file", "file": {"filename": "upload.pdf", "file_data": uri}}
                    if mime == "application/pdf" else {"type": "image_url", "image_url": {"url": uri}})
            content = [{"type": "text", "text": text}, part]
        payload = {
            "model": self.models[0],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "provider": provider,
        }
        if len(self.models) > 1:
            payload["models"] = self.models
        return payload

    async def interpret(self, context: str, message: str) -> dict:
        return await self._send(self._payload(context, message))

    async def complete(self, system: str, text: str, attachment: Attachment | None = None,
                       max_tokens: int = 4096) -> dict:
        return await self._send(self._body(system, text, attachment, max_tokens))

    async def _send(self, payload: dict) -> dict:
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                r = await self._client.post(OPENROUTER_URL, json=payload)
                if r.status_code == 402:
                    raise LLMBillingError(f"OpenRouter billing: {_error_message(r)}")
                if r.status_code >= 400:
                    raise LLMError(f"OpenRouter HTTP {r.status_code}: {_error_message(r)}")
                data = r.json()
                if "error" in data:
                    raise LLMError(f"OpenRouter error: {data['error']}")
                content = data["choices"][0]["message"]["content"] or ""
                u = data.get("usage") or {}
                _report(self.on_usage, data.get("model") or self.models[0],
                        u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                return extract_json(content)
            except LLMBillingError:
                raise
            except (httpx.HTTPError, LLMError, KeyError, ValueError) as e:
                last_err = e
                log.warning("LLM attempt %d failed: %s", attempt + 1, e)
        raise LLMError(str(last_err))

    async def aclose(self) -> None:
        await self._client.aclose()


def _report(cb: UsageCallback | None, model: str, input_tokens: int, output_tokens: int) -> None:
    cost = estimate_cost(model, input_tokens, output_tokens)
    log.info("LLM model=%s tokens in=%d out=%d est_cost=%s", model, input_tokens, output_tokens,
             f"${cost:.5f}" if cost is not None else "n/a")
    if cb:
        try:
            cb(model, input_tokens, output_tokens)
        except Exception:
            log.exception("usage callback failed")


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"]).strip()[:300]
    except Exception:
        return r.text[:300].replace("\n", " ")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiLLM:
    """Google Gemini API (AI Studio key). Tries each model in order until one answers.

    Privacy: only the paid tier (billing enabled on the key's project) keeps prompts
    out of training; the free tier does not.
    """

    def __init__(self, api_key: str, models: list[str], timeout: float = 90.0):
        self.models = models
        self.on_usage: UsageCallback | None = None
        self._client = httpx.AsyncClient(timeout=timeout, headers={"x-goog-api-key": api_key})

    def _payload(self, context: str, message: str) -> dict:
        return self._body(SYSTEM_PROMPT, f"{context}\n\nMESSAGE:\n{message}", None, 4096)

    def _body(self, system: str, text: str, attachment: Attachment | None, max_tokens: int) -> dict:
        parts: list[dict] = [{"text": text}]
        if attachment:
            data, mime = attachment
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}})
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                # Generous: on thinking models, reasoning tokens count against this limit.
                "maxOutputTokens": max_tokens,
            },
        }

    async def interpret(self, context: str, message: str) -> dict:
        return await self._send(self._payload(context, message))

    async def complete(self, system: str, text: str, attachment: Attachment | None = None,
                       max_tokens: int = 4096) -> dict:
        return await self._send(self._body(system, text, attachment, max_tokens))

    async def _send(self, payload: dict) -> dict:
        last_err: Exception | None = None
        for model in self.models:
            for attempt in range(2):
                try:
                    r = await self._client.post(GEMINI_URL.format(model=model), json=payload)
                    if r.status_code == 404:
                        raise LLMError(f"Gemini model {model!r} not found")
                    if r.status_code == 402:
                        raise LLMBillingError(f"Gemini billing: {_error_message(r)}")
                    if r.status_code >= 400:
                        raise LLMError(f"Gemini HTTP {r.status_code}: {_error_message(r)}")
                    data = r.json()
                    parts = data["candidates"][0]["content"]["parts"]
                    content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    u = data.get("usageMetadata") or {}
                    _report(self.on_usage, model, u.get("promptTokenCount", 0),
                            u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0))
                    return extract_json(content)
                except LLMBillingError as e:
                    log.error("%s", e)
                    raise
                except (httpx.HTTPError, LLMError, KeyError, IndexError, ValueError) as e:
                    last_err = e
                    log.warning("Gemini %s attempt %d failed: %s", model, attempt + 1, e)
                    if "not found" in str(e):
                        break  # no point retrying a missing model
        raise LLMError(str(last_err))

    async def list_models(self) -> list[str]:
        r = await self._client.get(GEMINI_MODELS_URL, params={"pageSize": 1000})
        r.raise_for_status()
        return [
            m["name"].removeprefix("models/") for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm(provider: str, api_key: str, models: list[str], zdr: bool = True):
    if provider == "gemini":
        return GeminiLLM(api_key, models)
    return OpenRouterLLM(api_key, models, zdr=zdr)
