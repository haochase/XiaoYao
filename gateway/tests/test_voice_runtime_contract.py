import pytest

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.memory import MemoryCategory, MemoryProposalCandidate
from companion_gateway.voice.runtime import ModelResponse, VoiceIntent


def test_model_response_defaults_to_no_memory_proposals() -> None:
    response = ModelResponse(
        text="你好",
        pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00"),
    )

    assert response.memory_proposals == ()


def test_model_response_carries_validated_memory_proposals() -> None:
    proposal = MemoryProposalCandidate(
        category=MemoryCategory.ROUTINE_PREFERENCE,
        value="Prefer a short morning reminder",
    )
    response = ModelResponse(
        text="好的",
        pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00"),
        memory_proposals=(proposal,),
    )

    assert response.memory_proposals == (proposal,)


def test_model_response_allows_deferred_audio_for_structured_intent() -> None:
    intent = VoiceIntent(type="current_time")

    response = ModelResponse(text="模型自由回复", pcm=None, intent=intent)

    assert response.intent == intent
    assert response.pcm is None


def test_model_response_allows_empty_text_for_structured_intent() -> None:
    response = ModelResponse(
        text="",
        pcm=None,
        intent=VoiceIntent(type="current_date"),
    )

    assert response.text == ""


def test_model_response_rejects_missing_audio_without_structured_intent() -> None:
    with pytest.raises(ValueError, match="pcm is required"):
        ModelResponse(text="没有音频", pcm=None)


def test_voice_intent_rejects_unknown_types() -> None:
    with pytest.raises(ValueError):
        VoiceIntent(type="weather")


def test_voice_intent_accepts_next_meeting() -> None:
    assert VoiceIntent(type="next_meeting").type == "next_meeting"


def test_project_query_intent_requires_a_query() -> None:
    intent = VoiceIntent(type="project_query", query="终端方案")

    assert intent.type == "project_query"
    assert intent.query == "终端方案"

    with pytest.raises(ValueError, match="query"):
        VoiceIntent(type="project_query")


def test_non_project_intent_rejects_a_project_query_field() -> None:
    with pytest.raises(ValueError, match="query only"):
        VoiceIntent(type="next_meeting", query="终端方案")
