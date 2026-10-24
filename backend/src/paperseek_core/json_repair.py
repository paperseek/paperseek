"""Close a JSON reply that a model ended a few brackets short."""

from __future__ import annotations

import json
from typing import Any


def close_brackets(text: str) -> str:
    """Append the closing brackets missing at the end of an otherwise complete JSON value.

    Some models (seen with Intern-S2) finish normally yet leave out the final ``}`` or ``]}``.
    Only brackets are added, never content: text that stops inside a string, or whose
    brackets do not pair up, is returned unchanged.
    """
    closers: list[str] = []
    in_string = escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            closers.append("]" if char == "[" else "}")
        elif char in "]}":
            if not closers or closers.pop() != char:
                return text
    if in_string or not closers:
        return text
    return text.rstrip().rstrip(",") + "".join(reversed(closers))


def loads_closing_brackets(text: str) -> Any:
    """``json.loads``, retried once with missing closing brackets added."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        closed = close_brackets(text)
        if closed == text:
            raise
        return json.loads(closed)
