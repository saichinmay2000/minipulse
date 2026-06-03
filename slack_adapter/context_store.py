"""
In-memory store for Slack thread conversation history.
Keyed by thread_ts (or message ts for root messages).
Each entry is a list of {role, content} dicts passed to the AI service.
"""

from typing import Dict, List

_store: Dict[str, List[dict]] = {}


def get(thread_ts: str) -> List[dict]:
    return _store.get(thread_ts, [])


def append(thread_ts: str, role: str, content: str) -> None:
    if thread_ts not in _store:
        _store[thread_ts] = []
    _store[thread_ts].append({"role": role, "content": content})


def clear(thread_ts: str) -> None:
    _store.pop(thread_ts, None)