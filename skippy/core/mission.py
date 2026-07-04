from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4


class MissionStatus(str, Enum):
    CREATED = "created"
    CONTEXT_LOADED = "context_loaded"
    PLANNED = "planned"
    VALIDATED = "validated"
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
    plan: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    confidence: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update_status(self, status: MissionStatus) -> None:
        self.status = status
        self.updated_at = datetime.utcnow()

    def add_result(self, result: dict[str, Any]) -> None:
        self.results.append(result)
        self.updated_at = datetime.utcnow()

    def add_lesson(self, lesson: str) -> None:
        self.lessons.append(lesson)
        self.updated_at = datetime.utcnow()
