"""
SKIPPY AI Collaboration Bridge — Simulated Agents.

LOCAL SYNTHETIC ONLY.
No external AI APIs, no repository writes, no trading execution.
"""

from __future__ import annotations

from typing import Any


class AgentError(ValueError):
    """Raised when a simulated agent cannot process a message."""


class SimulatedSkyppy:
    """
    Deterministic architecture agent.

    Produces assignments and reviews plans against a simple rule:
    a plan is acceptable only when it contains a non-empty "steps" list
    and does not contain the marker "needs_correction".
    """

    def assignment(self, objective: str) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip():
            raise AgentError("Objective is required")
        return {
            "objective": objective.strip(),
            "constraints": [
                "local simulation only",
                "no external APIs",
                "no repository writes",
            ],
        }

    def review(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise AgentError("Plan must be a dictionary")

        steps = plan.get("steps")
        needs_correction = bool(plan.get("needs_correction"))

        if needs_correction or not isinstance(steps, list) or not steps:
            return {
                "decision": "correction_required",
                "reason": "Plan is incomplete or marked for correction",
            }

        return {
            "decision": "approved",
            "reason": "Plan meets synthetic acceptance rules",
        }


class SimulatedJason:
    """
    Deterministic implementation agent.

    Produces a plan from an assignment. When asked to correct, returns
    a plan that satisfies Skyppy's acceptance rule.
    """

    def plan(self, assignment: dict[str, Any], correction: bool = False) -> dict[str, Any]:
        if not isinstance(assignment, dict):
            raise AgentError("Assignment must be a dictionary")

        objective = assignment.get("objective", "unspecified")

        if correction:
            return {
                "objective": objective,
                "steps": [
                    "review constraints",
                    "produce corrected plan",
                    "submit for review",
                ],
                "needs_correction": False,
            }

        # First response can be incomplete when the assignment requests it.
        if assignment.get("force_correction"):
            return {
                "objective": objective,
                "steps": [],
                "needs_correction": True,
            }

        return {
            "objective": objective,
            "steps": [
                "inspect protocol",
                "implement local flow",
                "add tests",
            ],
            "needs_correction": False,
        }
