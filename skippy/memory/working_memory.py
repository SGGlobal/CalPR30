"""
Working Memory

Temporary memory used while a mission is active.
Cleared after mission completion.
"""

from skippy.memory.working_memory import WorkingMemory
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkingMemory:
    mission_id: str = ""

    known_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)

    observations: list[dict[str, Any]] = field(default_factory=list)

    current_plan: list[str] = field(default_factory=list)

    tool_results: list[dict[str, Any]] = field(default_factory=list)

    confidence: float = 0.0

    def add_fact(self, fact: str):
        self.known_facts.append(fact)

    def add_unknown(self, item: str):
        self.unknowns.append(item)

    def add_hypothesis(self, hypothesis: str):
        self.hypotheses.append(hypothesis)

    def add_observation(self, observation: dict[str, Any]):
        self.observations.append(observation)

    def add_tool_result(self, result: dict[str, Any]):
        self.tool_results.append(result)

    def set_plan(self, steps: list[str]):
        self.current_plan = steps

    def clear(self):
        self.known_facts.clear()
        self.unknowns.clear()
        self.hypotheses.clear()
        self.observations.clear()
        self.current_plan.clear()
        self.tool_results.clear()
        self.confidence = 0.0
