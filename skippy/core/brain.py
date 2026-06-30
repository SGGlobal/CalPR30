"""
Skippy Brain

The Brain coordinates Skippy's major subsystems.
It does not contain intelligence itself—it orchestrates it.
"""

from skippy.memory.manager import Memory


class Brain:
    def __init__(self):
        self.memory = Memory()

    def observe(self, signal: dict):
        """Receive a new observation."""
        self.signal = signal

    def think(self):
        """Return similar historical experiences."""
        if not hasattr(self, "signal"):
            return []

        decision = self.signal.get("decision")
        return self.memory.search(decision=decision)

    def remember(self, decision: dict):
        """Placeholder for saving future decisions."""
        print("Remembering decision:", decision)


if __name__ == "__main__":
    brain = Brain()

    brain.observe({"decision": "WATCH"})

    matches = brain.think()

    print(f"Brain found {len(matches)} related memories.")
