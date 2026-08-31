from datetime import UTC, datetime

from companion_gateway.agent.reminder import parse_timed_reminder


def test_parse_today_chinese_time_and_message() -> None:
    result = parse_timed_reminder(
        "今天零点十二分提醒我去洗澡",
        now=datetime(2026, 8, 30, 16, 5, tzinfo=UTC),
    )

    assert result is not None
    message, scheduled = result
    assert message == "去洗澡"
    assert scheduled.isoformat() == "2026-08-31T00:12:00+08:00"
