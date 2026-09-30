"""Bounded document history shared by all editor operations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

EDITOR_HISTORY_LIMIT = 100


def history_stacks(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accept the current history format and the former single-snapshot format."""

    if "past" in state or "future" in state:
        # Stored revisions are immutable. Copy the stacks, not every document,
        # so a hundred-step history does not slow down every subsequent edit.
        return list(state.get("past", [])), list(state.get("future", []))
    if isinstance(state.get("document"), dict) or state.get("lines"):
        return [deepcopy(state)], []
    return [], []


def record_history(state: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Record one completed mutation; a no-op preserves the redo branch."""

    if snapshot is None:
        return state
    past, _future = history_stacks(state)
    if not past or past[-1] != snapshot:
        past.append(deepcopy(snapshot))
    return {"past": past[-EDITOR_HISTORY_LIMIT:], "future": []}


def travel_history(
    state: dict[str, Any], current: dict[str, Any], *, redo: bool = False
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Move exactly one document between the past and future stacks."""

    past, future = history_stacks(state)
    source, destination = (future, past) if redo else (past, future)
    if not source:
        return None, state
    restored = source.pop()
    destination.append(deepcopy(current))
    return deepcopy(restored), {
        "past": past[-EDITOR_HISTORY_LIMIT:],
        "future": future[-EDITOR_HISTORY_LIMIT:],
    }
