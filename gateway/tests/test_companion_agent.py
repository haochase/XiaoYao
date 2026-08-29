import json

from companion_gateway.agent.templates.companion import (
    CompanionSession,
    CompanionTurnResult,
    build_companion_system_prompt,
    parse_companion_turn,
)
from companion_gateway.domain.memory import MemoryCategory


def test_companion_turn_parses_emotion_and_memory_proposal() -> None:
    result = parse_companion_turn(
        json.dumps(
            {
                "reply": "听起来你今天有些累，我们慢慢聊。",
                "emotion": "tired",
                "memory_proposal": {
                    "category": "routine_preference",
                    "value": "晚上九点后更喜欢轻松聊天",
                },
                "end_session": False,
            },
            ensure_ascii=False,
        )
    )

    assert isinstance(result, CompanionTurnResult)
    assert result.emotion == "tired"
    assert result.memory_proposal is not None
    assert result.memory_proposal.category is MemoryCategory.ROUTINE_PREFERENCE
    assert result.end_session is False


def test_companion_invalid_structure_falls_back_without_side_effects() -> None:
    result = parse_companion_turn("我在这里，愿意听你说。")

    assert result.reply == "我在这里，愿意听你说。"
    assert result.emotion == "neutral"
    assert result.memory_proposal is None
    assert result.end_session is False


def test_companion_session_clear_removes_only_short_context() -> None:
    session = CompanionSession().append("user", "以后叫我小明").append(
        "assistant",
        "好的，不过需要你确认后我才会记住。",
    )

    cleared = session.clear()

    assert len(session.turns) == 2
    assert cleared.turns == ()


def test_companion_prompt_requires_confirmation_and_short_voice_reply() -> None:
    prompt = build_companion_system_prompt(max_turns=8)

    assert "memory_proposal" in prompt
    assert "explicit confirmation" in prompt
    assert "two or three sentences" in prompt
    assert "8" in prompt
