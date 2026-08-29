from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DailySummaryFacts:
    reminders: tuple[str, ...] = ()
    medications: tuple[str, ...] = ()
    agent_executions: tuple[str, ...] = ()
    companion_sessions: tuple[str, ...] = ()
    english_practice: tuple[str, ...] = ()


FactProvider = Callable[[str, datetime], DailySummaryFacts]


class DailySummaryBuilder:
    """Formats facts supplied by a read-only application-owned provider."""

    def __init__(self, *, facts_provider: FactProvider) -> None:
        self._facts_provider = facts_provider

    def __call__(self, owner_id: str, now: datetime) -> str:
        facts = self._facts_provider(owner_id, now)
        if not isinstance(facts, DailySummaryFacts):
            raise ValueError("daily summary facts provider returned an invalid value")
        return build_daily_summary(facts)


def build_daily_summary(facts: DailySummaryFacts) -> str:
    sections = (
        ("提醒", facts.reminders),
        ("服药", facts.medications),
        ("智能体执行", facts.agent_executions),
        ("陪伴", facts.companion_sessions),
        ("英语练习", facts.english_practice),
    )
    lines = ["今日小结："]
    for label, entries in sections:
        if entries:
            lines.append(f"{label}：{'；'.join(entries)}")
    if len(lines) == 1:
        lines.append("暂无已记录事项。")
    return "\n".join(lines)
