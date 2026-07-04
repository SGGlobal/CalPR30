"""
Mission

The Mission is the central unit of work in Skippy.
Every task the system performs is represented as a Mission.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class MissionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Mission:
    objective: str
    priority: int = 3

    id: str = field(default_factory=lambda: str(uuid4()))
    status: MissionStatus = MissionStatus.CREATED

    context: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)

    confidence: float = 0.0

    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def update_status(self, status: MissionStatus) -> None:
        self.status = status
        self.updated_at = utc_now()

    def add_result(self, result: dict[str, Any]) -> None:
        self.results.append(result)
        self.updated_at = utc_now()

    def add_lesson(self, lesson: str) -> None:
        self.lessons.append(lesson)
        self.updated_at = utc_now()
