"""
Hermes Reasoning Engine

Hermes transforms observations into decisions.
"""

from dataclasses import dataclass


@dataclass
class Observation:
    signal: dict


class Hermes:
    def observe(self, signal: dict):
        return Observation(signal)

    def recall(self, memory, observation):
        decision = observation.signal.get("decision")
        return memory.search(decision=decision)

    def reason(self, observation, memories):

        confidence = len(memories)

        return {
            "observation": observation.signal,
            "confidence": confidence,
            "decision": observation.signal.get("decision", "WATCH"),
        }
