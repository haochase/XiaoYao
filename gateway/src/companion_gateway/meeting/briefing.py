from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from time import perf_counter
from typing import Literal, Protocol

from companion_gateway.chat.service import TextChatTurn
from companion_gateway.meeting.models import MeetingEvent
from companion_gateway.voice.minicpm_o import ModelRuntimeError


_PREPARATION_PHRASES = {
    "review_agenda": "请提前查看议程",
    "prepare_materials": "请提前准备材料",
    "bring_notebook": "请带上笔记本",
    "arrive_early": "请提前到场",
    "none": "请提前准备",
}
logger = logging.getLogger(__name__)


class BriefingRuntime(Protocol):
    def respond(
        self,
        text: str,
        *,
        history: tuple[TextChatTurn, ...] = (),
    ) -> str: ...


@dataclass(frozen=True)
class BriefingResult:
    text: str
    mode: Literal["ai", "fallback"]


class MeetingBriefingService:
    def __init__(self, *, runtime: BriefingRuntime) -> None:
        self._runtime = runtime

    @classmethod
    def fallback_text(cls, event: MeetingEvent, *, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        minutes = max(1, ceil((event.start_at - now).total_seconds() / 60))
        return cls._fallback(event, minutes)

    def generate(self, event: MeetingEvent, *, now: datetime) -> BriefingResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        started_at = perf_counter()
        minutes = max(1, ceil((event.start_at - now).total_seconds() / 60))
        prompt = (
            "根据以下真实飞书日程选择一个会前准备标签。"
            "只返回以下标签之一，不能输出其他字符："
            "review_agenda|prepare_materials|bring_notebook|arrive_early|none\n"
            f"距离开始：{minutes}分钟\n标题：{event.summary}\n"
            f"地点：{event.location or '未填写'}\n描述：{event.description_excerpt}"
        )
        try:
            label = self._runtime.respond(prompt, history=())
        except (ModelRuntimeError, ValueError):
            result = BriefingResult(self._fallback(event, minutes), "fallback")
        else:
            if label not in _PREPARATION_PHRASES:
                result = BriefingResult(self._fallback(event, minutes), "fallback")
            else:
                result = BriefingResult(
                    self._compose(event, minutes, _PREPARATION_PHRASES[label]),
                    "ai",
                )
        duration_ms = max(0, int((perf_counter() - started_at) * 1000))
        logger.info(
            "meeting_briefing_generated mode=%s duration_ms=%d output_chars=%d",
            result.mode,
            duration_ms,
            len(result.text),
        )
        return result

    @staticmethod
    def _fallback(event: MeetingEvent, minutes: int) -> str:
        return MeetingBriefingService._compose(
            event,
            minutes,
            _PREPARATION_PHRASES["none"],
        )

    @staticmethod
    def _compose(event: MeetingEvent, minutes: int, preparation: str) -> str:
        prefix = f"提醒你，{minutes}分钟后参加"
        suffix = f"，{preparation}。"
        where = f"，地点是{event.location}" if event.location else ""
        with_location = f"{prefix}{event.summary}{where}{suffix}"
        if len(with_location) <= 80:
            return with_location

        without_location = f"{prefix}{event.summary}{suffix}"
        if len(without_location) <= 80:
            return without_location

        summary_budget = max(1, 80 - len(prefix) - len(suffix))
        return f"{prefix}{event.summary[:summary_budget]}{suffix}"
