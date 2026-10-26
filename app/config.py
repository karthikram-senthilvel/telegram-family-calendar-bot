"""Configuration loaded from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# Only models from major labs are allowed (privacy requirement).
ALLOWED_MODEL_PREFIXES = ("anthropic/", "openai/", "google/")


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


def _int_set(raw: str) -> set[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError as e:
                raise ConfigError(f"Telegram user IDs must be numbers, got {part!r}") from e
    return out


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _models(name: str, default: str) -> list[str]:
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]


def llm_settings() -> tuple[str, str, list[str]]:
    """(provider, api_key, models) from the environment, validated."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "gemini":
        models = _models("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.5-flash")
        bad = [m for m in models if not m.startswith("gemini-")]
        if bad:
            raise ConfigError(f"GEMINI_MODELS must be Gemini models, got {bad}")
        return provider, _required("GEMINI_API_KEY"), models
    if provider == "openrouter":
        models = _models("OPENROUTER_MODELS", "anthropic/claude-haiku-5.5,anthropic/claude-sonnet-5.5")
        for m in models:
            if not m.startswith(ALLOWED_MODEL_PREFIXES):
                raise ConfigError(
                    f"Model {m!r} is not from an allowed lab ({', '.join(ALLOWED_MODEL_PREFIXES)})"
                )
        return provider, _required("OPENROUTER_API_KEY"), models
    raise ConfigError("LLM_PROVIDER must be 'gemini' or 'openrouter'")


@dataclass
class Config:
    telegram_token: str
    allowed_user_ids: set[int]
    admin_user_ids: set[int]
    llm_provider: str
    llm_api_key: str
    llm_models: list[str]
    openrouter_zdr: bool
    tz: ZoneInfo
    calendar_backend: str
    data_dir: Path
    default_duration_minutes: int = 60
    stt_url: str | None = None
    voice_max_seconds: int = 120
    local_places: str = ""
    notify_on_start: bool = True
    # Google
    google_token_path: Path | None = None
    google_calendar_id: str | None = None
    # CalDAV (Radicale)
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: str | None = None
    caldav_calendar_name: str = "Family"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        allowed = _int_set(_required("ALLOWED_USER_IDS"))
        if not allowed:
            raise ConfigError("ALLOWED_USER_IDS must list at least one Telegram user ID")
        admins = _int_set(os.environ.get("ADMIN_USER_IDS", "")) or allowed

        provider, api_key, models = llm_settings()

        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        backend = os.environ.get("CALENDAR_BACKEND", "google").strip().lower()
        if backend not in ("google", "caldav"):
            raise ConfigError("CALENDAR_BACKEND must be 'google' or 'caldav'")

        cfg = cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=allowed,
            admin_user_ids=admins,
            llm_provider=provider,
            llm_api_key=api_key,
            llm_models=models,
            openrouter_zdr=_bool("OPENROUTER_ZDR", True),
            tz=ZoneInfo(os.environ.get("TIMEZONE", "America/Chicago")),
            calendar_backend=backend,
            data_dir=data_dir,
            default_duration_minutes=int(os.environ.get("DEFAULT_DURATION_MINUTES", "60")),
            notify_on_start=_bool("NOTIFY_ON_START", True),
            stt_url=os.environ.get("STT_URL", "http://stt:8000").strip() or None,
            voice_max_seconds=int(os.environ.get("VOICE_MAX_SECONDS", "120")),
            local_places=os.environ.get(
                "LOCAL_PLACES", "Plano, Frisco, McKinney, Allen, Prosper, Richardson, Dallas"
            ).strip(),
        )
        if backend == "google":
            cfg.google_token_path = Path(
                os.environ.get("GOOGLE_TOKEN_PATH", str(data_dir / "google" / "token.json"))
            )
            cfg.google_calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip() or None
        else:
            cfg.caldav_url = _required("CALDAV_URL")
            cfg.caldav_username = _required("CALDAV_USERNAME")
            cfg.caldav_password = _required("CALDAV_PASSWORD")
            cfg.caldav_calendar_name = os.environ.get("CALDAV_CALENDAR_NAME", "Family")
        return cfg
