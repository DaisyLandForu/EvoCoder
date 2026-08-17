"""Deterministic OpenAI-compatible responses for CI. No live API."""

from __future__ import annotations

import json
from typing import Any


def _tool_response(tool_call_id: str, name: str, arguments: dict[str, Any], usage: dict[str, int]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage,
    }


def _text_response(text: str, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def script_for_task(task_id: str) -> list[dict[str, Any]]:
    if task_id == "se-write-hello":
        return [
            _tool_response(
                "call-write-hello",
                "write_file",
                {"file_path": "hello.py", "content": "print('Hello')\n"},
                {"prompt_tokens": 20, "completion_tokens": 8},
            ),
            _text_response("Created hello.py", {"prompt_tokens": 24, "completion_tokens": 6}),
        ]
    if task_id == "se-edit-app":
        return [
            _tool_response(
                "call-read-app",
                "read_file",
                {"file_path": "src/app.py"},
                {"prompt_tokens": 18, "completion_tokens": 5},
            ),
            _tool_response(
                "call-edit-app",
                "edit_file",
                {
                    "file_path": "src/app.py",
                    "old_string": "print('ok')\n",
                    "new_string": "print('ready')\n",
                },
                {"prompt_tokens": 22, "completion_tokens": 10},
            ),
            _text_response("Updated src/app.py", {"prompt_tokens": 26, "completion_tokens": 5}),
        ]
    if task_id == "se-read-app":
        return [
            _tool_response(
                "call-read-app",
                "read_file",
                {"file_path": "src/app.py"},
                {"prompt_tokens": 18, "completion_tokens": 6},
            ),
            _text_response("src/app.py prints ok", {"prompt_tokens": 30, "completion_tokens": 8}),
        ]
    if task_id == "se-wrong-output":
        return [
            _tool_response(
                "call-write-wrong",
                "write_file",
                {"file_path": "hello.py", "content": "print('Nope')\n"},
                {"prompt_tokens": 16, "completion_tokens": 7},
            ),
            _text_response("Wrote hello.py", {"prompt_tokens": 20, "completion_tokens": 4}),
        ]
    raise KeyError(f"no scripted responses for task {task_id}")


class ScriptedStream:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.index = 0

    async def __call__(self) -> dict[str, Any]:
        if self.index >= len(self.steps):
            return _text_response("(script exhausted)", {"prompt_tokens": 1, "completion_tokens": 1})
        step = self.steps[self.index]
        self.index += 1
        return step
