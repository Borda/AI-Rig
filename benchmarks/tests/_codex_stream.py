"""Codex ``exec --json`` event streams for the no-model runner tests."""

from __future__ import annotations

import json
from typing import Any


def completed_stream(
    *,
    output: str = "fixture answer",
    input_tokens: int = 10,
    cached_input_tokens: int = 0,
    output_tokens: int = 2,
    commands: list[dict[str, Any]] | None = None,
) -> str:
    """Build one official-shape completed Codex event stream.

    Example:
        >>> events = [json.loads(line) for line in completed_stream(output="example", input_tokens=0).splitlines()]
        >>> [event["type"] for event in events]
        ['thread.started', 'item.completed', 'turn.completed']
        >>> events[1]["item"]["text"], events[-1]["usage"]["input_tokens"]
        ('example', 0)
    """
    events: list[dict[str, Any]] = [{"type": "thread.started", "thread_id": "fixture-thread"}]
    events.extend(
        {"type": "item.completed", "item": {"id": f"command-{index}", **command}}
        for index, command in enumerate(commands or [], start=1)
    )
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": output},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
    )
    return "\n".join(json.dumps(event) for event in events)
