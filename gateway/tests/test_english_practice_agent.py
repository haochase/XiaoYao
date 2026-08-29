import json

from companion_gateway.agent.templates.english import (
    EnglishPracticeSession,
    build_english_system_prompt,
    parse_english_turn,
)


def english_result(*, session_complete: bool = False) -> str:
    return json.dumps(
        {
            "heard_text": "I go to work yesterday.",
            "coach_reply_en": "What did you do after work?",
            "feedback_zh": "过去发生的事情要使用过去时。",
            "corrections": [
                {
                    "original": "I go to work yesterday.",
                    "corrected": "I went to work yesterday.",
                    "reason_zh": "go 的过去式是 went。",
                }
            ],
            "scores": {"grammar": 3, "vocabulary": 3, "relevance": 5},
            "suggested_expression": "After work, I...",
            "session_complete": session_complete,
        },
        ensure_ascii=False,
    )


def test_english_turn_parses_correction_and_bounded_scores() -> None:
    result = parse_english_turn(english_result())

    assert result.heard_text == "I go to work yesterday."
    assert result.corrections[0].corrected == "I went to work yesterday."
    assert result.scores is not None
    assert result.scores.grammar == 3
    assert result.scores.relevance == 5


def test_english_session_finishes_at_five_turns() -> None:
    session = EnglishPracticeSession(
        level="intermediate",
        scenario="interview",
    )

    for _ in range(5):
        session = session.advance(parse_english_turn(english_result()))

    assert session.turn_count == 5
    assert session.completed is True
    assert len(session.corrections) == 5


def test_english_invalid_structure_does_not_invent_scores() -> None:
    result = parse_english_turn("Could you tell me about your experience?")

    assert result.coach_reply_en == "Could you tell me about your experience?"
    assert result.scores is None
    assert result.corrections == ()


def test_english_missing_required_json_fields_uses_safe_fallback() -> None:
    raw = '{"coach_reply_en":"Try again","scores":{"grammar":9}}'

    result = parse_english_turn(raw)

    assert result.coach_reply_en == "Try again"
    assert result.coach_reply_en != raw
    assert result.scores is None
    assert result.corrections == ()


def test_english_advance_at_turn_limit_never_creates_a_sixth_turn() -> None:
    session = EnglishPracticeSession(
        level="intermediate",
        scenario="interview",
        turn_count=5,
        max_turns=5,
        completed=False,
    )

    result = session.advance(parse_english_turn(english_result()))

    assert result.turn_count == 5
    assert result.completed is True


def test_text_practice_prompt_forbids_pronunciation_score() -> None:
    prompt = build_english_system_prompt(
        level="beginner",
        scenario="travel",
        input_mode="text",
        max_turns=5,
    )

    assert "pronunciation" in prompt
    assert "must not" in prompt
    assert "travel" in prompt
    assert "5" in prompt
