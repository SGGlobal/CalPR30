"""
Brain Coordinator

Coordinates Skippy's major subsystems.
"""

from skippy.core.brain import Brain
from skippy.reasoning.hermes import Hermes


class BrainCoordinator:
    def __init__(self):
        self.brain = Brain()
        self.hermes = Hermes()

    def run(self, signal: dict):
        """
        Execute one reasoning cycle.
        """

        # Observe the incoming signal
        self.brain.observe(signal)

        # Recall similar experiences
        memories = self.brain.think()

        # Build an observation
        observation = self.hermes.observe(signal)

        # Produce a decision
        decision = self.hermes.reason(observation, memories)

        return decision


if __name__ == "__main__":
    coordinator = BrainCoordinator()

    result = coordinator.run({"decision": "WATCH", "market": "BTC"})

    print(result)
