from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from threading import RLock
from typing import TYPE_CHECKING, Callable

from companion_gateway.domain.agents import AgentKind, AgentSpec
from companion_gateway.agent.reminder import parse_timed_reminder

if TYPE_CHECKING:
    from companion_gateway.agent.registry import AgentRegistry
    from companion_gateway.agent.runtime import AgentRuntime


Clock = Callable[[], datetime]
logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class AgentRouteResult:
    handled: bool
    reply: str | None


class AgentCommandRouter:
    """Owner- and chat-scoped command state for narrow Feishu Agent management."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        runtime: AgentRuntime,
        clock: Clock,
        reminder_tool: object | None = None,
        target_device_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._clock = clock
        self._reminder_tool = reminder_tool
        self._target_device_id = target_device_id
        self._pending: dict[tuple[str, str], str] = {}
        self._active: dict[tuple[str, str], AgentSpec] = {}
        self._lock = RLock()

    def handle(
        self,
        *,
        text: str,
        owner_id: str,
        chat_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        normalized = text.strip()
        if not normalized:
            return AgentRouteResult(handled=False, reply=None)
        key = (owner_id, chat_id)
        try:
            with self._lock:
                return self._handle(
                    key=key,
                    text=normalized,
                    owner_id=owner_id,
                    source_message_id=source_message_id,
                )
        except Exception:
            logger.exception("agent_command_failed")
            return AgentRouteResult(handled=True, reply="智能体命令执行失败，请稍后重试。")

    def active_context(self, *, owner_id: str, chat_id: str) -> str:
        with self._lock:
            active = self._active.get((owner_id, chat_id))
            if active is None:
                return ""
            return _active_context(active)

    def active_context_for_owner(self, *, owner_id: str) -> str:
        """Return a voice-safe context only when the owner has one active mode."""
        with self._lock:
            active_agents = {
                agent.agent_id: agent
                for (active_owner_id, _chat_id), agent in self._active.items()
                if active_owner_id == owner_id
            }
            if len(active_agents) != 1:
                return ""
            return _active_context(next(iter(active_agents.values())))

    def _handle(
        self,
        *,
        key: tuple[str, str],
        text: str,
        owner_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        create_prefix = "\u521b\u5efa\u4e00\u4e2a"
        if text.startswith(create_prefix):
            request_text = text[len(create_prefix) :].strip()
            if not request_text:
                return AgentRouteResult(True, "请描述需要创建的智能体。")
            draft = self._registry.propose(
                request_text,
                owner_id=owner_id,
                source_message_id=source_message_id,
            )
            self._pending[key] = draft.draft_id
            return AgentRouteResult(
                True,
                f"已生成“{draft.spec.name}”草稿。回复“确认创建”保存，"
                "或回复“取消创建”放弃。",
            )
        reminder = parse_timed_reminder(text, now=self._clock())
        if reminder is not None and self._reminder_tool is not None:
            message, scheduled = reminder
            if self._target_device_id is None:
                return AgentRouteResult(True, "提醒设备尚未配置。")
            result = self._reminder_tool.create_reminder(
                actor_id=owner_id,
                target_device_id=self._target_device_id,
                arguments={
                    "text": message,
                    "idempotency_key": f"feishu-reminder-{source_message_id}",
                    "schedule": {
                        "at": scheduled.isoformat(),
                        "timezone": "Asia/Shanghai",
                    },
                },
                trace_id=f"feishu-reminder-{source_message_id}",
            )
            created = result.result.get("created") is True
            return AgentRouteResult(
                True,
                f"{'已创建' if created else '提醒已存在'}提醒，将于"
                f"{scheduled:%m月%d日 %H:%M}提醒你{message}。",
            )
        if _looks_like_timed_reminder(text) and self._reminder_tool is not None:
            return AgentRouteResult(True, "提醒时间无法识别，请使用例如“今天 20 点提醒我喝水”。")
        if _looks_like_timed_reminder(text):
            draft = self._registry.propose(
                text,
                owner_id=owner_id,
                source_message_id=source_message_id,
            )
            self._pending[key] = draft.draft_id
            return AgentRouteResult(
                True,
                f"已识别提醒“{draft.spec.name}”草稿。回复“确认创建”保存，"
                "或回复“取消创建”放弃。",
            )
        if text == "\u786e\u8ba4\u521b\u5efa":
            draft_id = self._pending.get(key)
            if draft_id is None:
                return AgentRouteResult(True, "当前没有待确认的智能体草稿。")
            agent = self._registry.confirm(draft_id, owner_id=owner_id)
            self._pending.pop(key, None)
            return AgentRouteResult(True, f"已创建智能体“{agent.name}”。")
        if text == "\u53d6\u6d88\u521b\u5efa":
            if self._pending.pop(key, None) is None:
                return AgentRouteResult(True, "当前没有待确认的智能体草稿。")
            return AgentRouteResult(True, "已取消创建智能体。")
        if text == "\u6211\u7684\u667a\u80fd\u4f53":
            agents = self._registry.list(owner_id=owner_id)
            if not agents:
                return AgentRouteResult(True, "当前还没有智能体。")
            details = ", ".join(
                f"{agent.name}（{'已启用' if agent.enabled else '已暂停'}）"
                for agent in agents
            )
            return AgentRouteResult(True, f"我的智能体：{details}")
        for command, action in (
            ("\u8fd0\u884c", self._run),
            ("\u6682\u505c", self._pause),
            ("\u6062\u590d", self._resume),
            ("\u5220\u9664", self._delete),
        ):
            prefix = f"{command} "
            if text.startswith(prefix):
                name = text[len(prefix) :].strip()
                if not name:
                    return AgentRouteResult(True, "请提供智能体名称。")
                agent, error = self._agent_by_name(owner_id=owner_id, name=name)
                if error is not None:
                    return AgentRouteResult(True, error)
                return action(
                    key=key,
                    agent=agent,
                    owner_id=owner_id,
                    source_message_id=source_message_id,
                )
        if text == "\u8fdb\u5165\u966a\u4f34\u6a21\u5f0f":
            return self._activate_kind(key=key, owner_id=owner_id, kind=AgentKind.COMPANION)
        if text.startswith("\u5f00\u59cb") and text.endswith("\u82f1\u8bed\u7ec3\u4e60"):
            return self._activate_english(key=key, owner_id=owner_id, text=text)
        if text == "\u9000\u51fa\u5f53\u524d\u6a21\u5f0f":
            if self._active.pop(key, None) is None:
                return AgentRouteResult(True, "当前没有正在使用的智能体模式。")
            return AgentRouteResult(True, "已退出当前智能体模式。")
        active = self._active.get(key)
        if active is not None:
            return AgentRouteResult(handled=False, reply=None)
        return AgentRouteResult(handled=False, reply=None)

    def _agent_by_name(
        self,
        *,
        owner_id: str,
        name: str,
    ) -> tuple[AgentSpec | None, str | None]:
        matches = [
            agent
            for agent in self._registry.list(owner_id=owner_id)
            if agent.name == name
        ]
        if not matches:
            return None, "没有找到该智能体。"
        if len(matches) != 1:
            return None, "存在同名智能体，请先删除或重命名。"
        return matches[0], None

    def _run(
        self,
        *,
        key: tuple[str, str],
        agent: AgentSpec | None,
        owner_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        assert agent is not None
        trigger_id = _manual_trigger_id(agent.agent_id, source_message_id)
        execution = self._runtime.run(
            agent.agent_id,
            owner_id=owner_id,
            trigger_id=trigger_id,
            now=self._clock(),
        )
        if execution.status.value == "succeeded" and execution.output_text:
            return AgentRouteResult(True, execution.output_text)
        return AgentRouteResult(True, "智能体运行失败，请查看执行记录。")

    def _pause(
        self,
        *,
        key: tuple[str, str],
        agent: AgentSpec | None,
        owner_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        assert agent is not None
        self._registry.pause(agent.agent_id, owner_id=owner_id)
        return AgentRouteResult(True, f"已暂停智能体“{agent.name}”。")

    def _resume(
        self,
        *,
        key: tuple[str, str],
        agent: AgentSpec | None,
        owner_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        assert agent is not None
        self._registry.resume(agent.agent_id, owner_id=owner_id)
        return AgentRouteResult(True, f"已恢复智能体“{agent.name}”。")

    def _delete(
        self,
        *,
        key: tuple[str, str],
        agent: AgentSpec | None,
        owner_id: str,
        source_message_id: str,
    ) -> AgentRouteResult:
        assert agent is not None
        deleted = self._registry.delete(agent.agent_id, owner_id=owner_id)
        if deleted:
            active = self._active.get(key)
            if active is not None and active.agent_id == agent.agent_id:
                self._active.pop(key, None)
            return AgentRouteResult(True, f"已删除智能体“{agent.name}”。")
        return AgentRouteResult(True, "没有找到该智能体。")

    def _activate_kind(
        self,
        *,
        key: tuple[str, str],
        owner_id: str,
        kind: AgentKind,
    ) -> AgentRouteResult:
        matches = [
            agent
            for agent in self._registry.list(owner_id=owner_id)
            if agent.kind is kind and agent.enabled
        ]
        if not matches:
            return AgentRouteResult(True, "请先创建并启用一个陪伴智能体。")
        if len(matches) != 1:
            return AgentRouteResult(True, "存在多个陪伴智能体，请只保留一个启用项。")
        self._active[key] = matches[0]
        return AgentRouteResult(True, "已进入陪伴模式，可以继续和小瑶聊天。")

    def _activate_english(
        self,
        *,
        key: tuple[str, str],
        owner_id: str,
        text: str,
    ) -> AgentRouteResult:
        request = text[len("\u5f00\u59cb") : -len("\u82f1\u8bed\u7ec3\u4e60")]
        levels = {
            "\u521d\u7ea7": "beginner",
            "\u4e2d\u7ea7": "intermediate",
            "\u9ad8\u7ea7": "advanced",
        }
        scenarios = {
            "\u65e5\u5e38": "daily",
            "\u65c5\u884c": "travel",
            "\u5496\u5561\u5e97": "cafe",
            "\u804c\u573a": "workplace",
            "\u9762\u8bd5": "interview",
        }
        for label, level in levels.items():
            if request.startswith(label):
                scenario = scenarios.get(request[len(label) :])
                if scenario is None:
                    return AgentRouteResult(True, "暂不支持这个英语练习场景。")
                matches = [
                    agent
                    for agent in self._registry.list(owner_id=owner_id)
                    if agent.kind is AgentKind.ENGLISH
                    and agent.enabled
                    and agent.config.get("level") == level
                    and agent.config.get("scenario") == scenario
                ]
                if len(matches) != 1:
                    return AgentRouteResult(
                        True,
                        "请先创建并启用对应等级和场景的英语练习智能体。",
                    )
                self._active[key] = matches[0]
                return AgentRouteResult(True, "英语练习已开始，请直接用英语回答。")
        return AgentRouteResult(True, "暂不支持这个英语练习等级。")


def _manual_trigger_id(agent_id: str, source_message_id: str) -> str:
    value = f"{agent_id}\x00{source_message_id}".encode("utf-8")
    return f"manual-{sha256(value).hexdigest()[:32]}"


def _looks_like_timed_reminder(text: str) -> bool:
    return (
        "提醒我" in text
        and any(token in text for token in ("今天", "明天", "后天"))
        and any(token in text for token in ("点", "时", ":"))
    )


def _active_context(agent: AgentSpec) -> str:
    if agent.kind is AgentKind.COMPANION:
        return "Gateway active mode: companion. Continue a supportive conversation."
    if agent.kind is AgentKind.ENGLISH:
        level = agent.config.get("level")
        scenario = agent.config.get("scenario")
        if level in {"beginner", "intermediate", "advanced"} and scenario in {
            "daily",
            "travel",
            "cafe",
            "workplace",
            "interview",
        }:
            return f"Gateway active mode: English practice level={level}; scenario={scenario}."
        return "Gateway active mode: English practice."
    return ""
