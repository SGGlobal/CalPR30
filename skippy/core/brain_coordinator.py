"""
Brain Coordinator

Coordinates Skippy's major subsystems.
"""

from skippy.core.brain import Brain
from skippy.reasoning.hermes import Hermes
from skippy.core.mission import Mission, MissionStatus
from skippy.memory.working_memory import WorkingMemory
from skippy.kernel.event_bus import EventBus
from skippy.kernel.events import Events


class BrainCoordinator:
    def __init__(self):
        self.brain = Brain()
        self.hermes = Hermes()
        self.working_memory = WorkingMemory()
        self.event_bus = EventBus()

    def run(self, mission: Mission) -> Mission:

        self.event_bus.publish(
            Events.MISSION_STARTED,
            {"mission": mission.objective},
        )

        mission.update_status(MissionStatus.RUNNING)

        self.working_memory.mission_id = mission.id
        self.working_memory.add_fact(f"Mission: {mission.objective}")
        self.working_memory.add_observation(mission.context)

        signal = mission.context

        self.event_bus.publish(Events.MEMORY_SEARCH_STARTED)

        self.brain.observe(signal)
        memories = self.brain.think()

        self.event_bus.publish(
            Events.MEMORY_SEARCH_COMPLETED,
            {"matches": len(memories)},
        )

        self.event_bus.publish(Events.REASONING_STARTED)

        observation = self.hermes.observe(signal)
        decision = self.hermes.reason(observation, memories)

        self.event_bus.publish(
            Events.REASONING_COMPLETED,
            {"confidence": decision["confidence"]},
        )

        self.working_memory.confidence = decision["confidence"]
        self.working_memory.add_tool_result(decision)

        mission.confidence = decision["confidence"]
        mission.add_result(decision)
        mission.update_status(MissionStatus.COMPLETED)

        self.event_bus.publish(
            Events.MISSION_COMPLETED,
            {"status": mission.status.value},
        )

        self.working_memory.clear()

        return mission


if __name__ == "__main__":
    coordinator = BrainCoordinator()

    coordinator.event_bus.subscribe(
        Events.MISSION_STARTED,
        lambda p: print("EVENT:", p),
    )

    coordinator.event_bus.subscribe(
        Events.MISSION_COMPLETED,
        lambda p: print("EVENT:", p),
    )

    mission = Mission(objective="Evaluate BTC Market")

    mission.context = {
        "decision": "WATCH",
        "market": "BTC",
    }

    result = coordinator.run(mission)

    print()
    print("Mission:", result.objective)
    print("Status:", result.status)
    print("Confidence:", result.confidence)
