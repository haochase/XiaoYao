from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.memory import MemoryCategory, MemoryProposalCandidate
from companion_gateway.voice.runtime import ModelResponse


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
