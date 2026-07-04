"""
Tool Registry

Capability-based tool access for Skippy Kernel v1.
"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    capability: str
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.capability] = tool

    def get(self, capability: str) -> Tool | None:
        return self._tools.get(capability)

    def capabilities(self) -> list[str]:
        return sorted(self._tools.keys())

    def execute(self, capability: str, *args, **kwargs) -> Any:
        tool = self.get(capability)

        if tool is None:
            raise ValueError(f"No tool registered for capability '{capability}'.")

        return tool.handler(*args, **kwargs)


if __name__ == "__main__":
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="Echo Tool",
            capability="echo",
            handler=lambda text: f"Skippy heard: {text}",
        )
    )

    print("Capabilities:", registry.capabilities())
    print(registry.execute("echo", "Hello Chief"))
