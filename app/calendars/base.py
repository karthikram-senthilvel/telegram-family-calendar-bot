from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Event


class CalendarBackend(ABC):
    """Synchronous interface; the bot calls these via asyncio.to_thread."""

    name: str = "calendar"

    @abstractmethod
    def create(self, ev: Event) -> Event: ...

    @abstractmethod
    def update(self, ev: Event) -> Event: ...

    @abstractmethod
    def delete(self, event_id: str) -> None: ...

    @abstractmethod
    def get(self, event_id: str) -> Event | None: ...

    @abstractmethod
    def list_between(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]: ...

    def check(self) -> None:
        """Raise if the calendar is unreachable. Used at startup."""
        self.list_between(datetime.now().astimezone(), datetime.now().astimezone(), limit=1)
