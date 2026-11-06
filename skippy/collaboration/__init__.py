"""
SKIPPY AI Collaboration Bridge — local protocol.

Development-only. No external APIs, no trading execution.
"""

from skippy.collaboration.protocol import (
    CollaborationProtocol,
    Message,
    ProtocolError,
    TaskState,
)

__all__ = [
    "CollaborationProtocol",
    "Message",
    "ProtocolError",
    "TaskState",
]
