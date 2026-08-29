from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256

from companion_gateway.agent.tools.weather import WeatherTool
from companion_gateway.domain.agents import (
    AgentChannel,
    AgentExecution,
    AgentExecutionStatus,
    AgentKind,
    AgentRepository,
    AgentSpec,
    AgentToolName,
)


Delivery = Callable[[str], bool]
SummaryBuilder = Callable[[str, datetime], str]


class AgentRuntimePolicyError(ValueError):
    """A safe, gateway-authored policy error suitable for user-visible status."""


class AgentRuntime:
    """Runs one persisted Agent through deterministic fact and delivery steps."""

    def __init__(
        self,
        *,
        repository: AgentRepository,
        weather_tool: WeatherTool,
        send_feishu: Delivery,
        speak_esp32: Delivery,
        summary_builder: SummaryBuilder,
    ) -> None:
        self._repository = repository
        self._weather_tool = weather_tool
        self._send_feishu = send_feishu
        self._speak_esp32 = speak_esp32
        self._summary_builder = summary_builder

    def run(
        self,
        agent_id: str,
        *,
        owner_id: str,
        trigger_id: str,
        now: datetime,
    ) -> AgentExecution:
        _require_aware(now)
        agent = self._repository.get_agent(agent_id, owner_id=owner_id)
        if agent is None:
            raise KeyError(agent_id)
        if not agent.enabled:
            raise ValueError("agent is disabled")

        started = AgentExecution(
            execution_id=execution_id_for(
                agent_id=agent.agent_id,
                trigger_id=trigger_id,
            ),
            agent_id=agent.agent_id,
            trigger_id=trigger_id,
            status=AgentExecutionStatus.STARTED,
            started_at=now,
        )
        claimed, created = self._repository.claim_execution(
            started,
            owner_id=owner_id,
        )
        if not created:
            return claimed
        try:
            output_text = self._build_output(agent, now=now)
            succeeded = started.model_copy(
                update={
                    "status": AgentExecutionStatus.SUCCEEDED,
                    "completed_at": now,
                    "output_text": output_text,
                    "error": None,
                }
            )
            self._repository.record_execution(succeeded, owner_id=owner_id)
            delivery_errors = self._deliver(agent, output_text)
            if not delivery_errors:
                return succeeded
            failed = succeeded.model_copy(
                update={
                    "status": AgentExecutionStatus.FAILED,
                    "completed_at": now,
                    "error": "; ".join(delivery_errors),
                }
            )
        except Exception as exc:
            failed = started.model_copy(
                update={
                    "status": AgentExecutionStatus.FAILED,
                    "completed_at": now,
                    "error": _error_text(exc),
                }
            )
        self._repository.record_execution(failed, owner_id=owner_id)
        return failed

    def _build_output(self, agent: AgentSpec, *, now: datetime) -> str:
        if agent.kind is AgentKind.WEATHER:
            _require_tool(agent, AgentToolName.WEATHER_FORECAST)
            city = _config_text(agent, "city")
            return _weather_text(self._weather_tool.advise(city, now=now))
        if agent.kind in {AgentKind.REMINDER, AgentKind.MEDICATION}:
            return _config_text(agent, "message")
        if agent.kind in {AgentKind.COMPANION, AgentKind.ENGLISH}:
            return agent.prompt
        if agent.kind is AgentKind.DAILY_SUMMARY:
            _require_tool(agent, AgentToolName.DAILY_SUMMARY)
            summary = self._summary_builder(agent.owner_id, now)
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("daily summary builder returned an empty result")
            return summary.strip()
        raise ValueError(f"agent kind is not supported: {agent.kind.value}")

    def _deliver(self, agent: AgentSpec, text: str) -> tuple[str, ...]:
        errors: list[str] = []
        if AgentChannel.FEISHU in agent.channels:
            if AgentToolName.SEND_FEISHU not in agent.allowed_tools:
                errors.append("Feishu tool is not allowed")
            elif not _delivery_succeeded(self._send_feishu, text):
                errors.append("Feishu delivery failed")
        if AgentChannel.ESP32 in agent.channels:
            if AgentToolName.SPEAK_ESP32 not in agent.allowed_tools:
                errors.append("ESP32 tool is not allowed")
            elif not _delivery_succeeded(self._speak_esp32, text):
                errors.append("ESP32 delivery failed")
        return tuple(errors)


def _config_text(agent: AgentSpec, field: str) -> str:
    value = agent.config.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{agent.kind.value} agent requires config.{field}")
    return value.strip()


def _require_tool(agent: AgentSpec, tool: AgentToolName) -> None:
    if tool not in agent.allowed_tools:
        raise AgentRuntimePolicyError(f"agent tool not allowed: {tool.value}")


def _delivery_succeeded(delivery: Delivery, text: str) -> bool:
    try:
        return delivery(text) is True
    except Exception:
        return False


def execution_id_for(*, agent_id: str, trigger_id: str) -> str:
    identity = f"{agent_id}\x00{trigger_id}".encode("utf-8")
    return f"execution-{sha256(identity).hexdigest()[:32]}"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, AgentRuntimePolicyError):
        return str(exc)
    return f"agent execution failed: {type(exc).__name__}"


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("agent runtime now must be timezone-aware")


def _weather_text(advice: object) -> str:
    city = getattr(advice, "city")
    observed_at = getattr(advice, "observed_at")
    temperature_c = getattr(advice, "temperature_c")
    apparent_temperature_c = getattr(advice, "apparent_temperature_c")
    precipitation_probability = getattr(advice, "precipitation_probability")
    wind_speed_kmh = getattr(advice, "wind_speed_kmh")
    clothing = getattr(advice, "clothing")
    carry_umbrella = getattr(advice, "carry_umbrella")
    umbrella = "建议带伞" if carry_umbrella else "无需带伞"
    return (
        f"{city} {observed_at.isoformat()}：气温 {temperature_c:g}C，"
        f"体感 {apparent_temperature_c:g}C，降水概率 {precipitation_probability}% ，"
        f"风速 {wind_speed_kmh:g} km/h。穿衣建议：{'、'.join(clothing)}；{umbrella}。"
    )
