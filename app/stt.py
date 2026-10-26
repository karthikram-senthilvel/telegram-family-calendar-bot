"""Client for the local speech-to-text container (stt/server.py)."""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class STTError(RuntimeError):
    pass


class STT(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


class STTClient:
    def __init__(self, base_url: str, timeout: float = 90.0):
        self.url = base_url.rstrip("/") + "/transcribe"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def transcribe(self, audio: bytes) -> str:
        try:
            r = await self._client.post(
                self.url, content=audio, headers={"Content-Type": "application/octet-stream"}
            )
        except httpx.HTTPError as e:
            raise STTError(f"speech-to-text unreachable: {e}") from e
        if r.status_code != 200:
            raise STTError(f"speech-to-text HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        log.info("voice: %.1fs audio transcribed in %d ms", data.get("seconds", 0), data.get("ms", 0))
        return str(data.get("text") or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()
