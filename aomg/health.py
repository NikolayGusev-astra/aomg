"""Health-модель: состояние, агрегат для иконки."""
from __future__ import annotations

from dataclasses import dataclass, field

WORST_ORDER = ["ok", "reconnecting", "channel_down", "down"]


@dataclass
class Health:
    state: str = "reconnecting"   # ok | reconnecting | channel_down | down
    tools: int = 0
    pid: int | None = None
    error: str | None = None
    last_check: float = 0.0
    history: list = field(default_factory=list)

    def record(self, state: str, tools: int = 0, error: str | None = None,
               ts: float = 0.0) -> None:
        self.state = state
        self.tools = tools
        self.error = error
        self.last_check = ts
        self.history.append({"state": state, "tools": tools, "ts": ts})
        del self.history[:-100]


def aggregate_state(healths: list[Health]) -> str:
    worst = "ok"
    for h in healths:
        if WORST_ORDER.index(h.state) > WORST_ORDER.index(worst):
            worst = h.state
    return worst
