"""
Brain Coordinator

Coordinates Skippy's major subsystems.
"""

from skippy.core.brain import Brain
from skippy.reasoning.hermes import Hermes
from skippy.core.mission import Mission, MissionStatus


class BrainCoordinator:
    def __init__(self):
        self.brain = Brain()
        self.hermes = Hermes()

    def run(self, mission: Mission) -> Mission:
        """Execute one reasoning cycle."""

        # Mission has started
        mission.update_status(MissionStatus.RUNNING)

        # Build the signal from mission context
        signal = mission.context

        # Observe
        self.brain.observe(signal)

        # Recall similar memories
        memories = self.brain.think()

        # Hermes reasoning
        observation = self.hermes.observe(signal)
        decision = self.hermes.reason(observation, memories)

        # Store results
        mission.confidence = decision["confidence"]
        mission.add_result(decision)

        # Mission complete
        mission.update_status(MissionStatus.COMPLETED)

        return mission


if __name__ == "__main__":
    coordinator = BrainCoordinator()

    mission = Mission(objective="Evaluate BTC Market")

    mission.context = {"decision": "WATCH", "market": "BTC"}

    result = coordinator.run(mission)

    print()
    print("Mission:", result.objective)
    print("Status:", result.status)
    print("Confidence:", result.confidence)
    print("Results:", result.results)
