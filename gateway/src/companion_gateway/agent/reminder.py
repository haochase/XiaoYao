from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def parse_timed_reminder(text: str, *, now: datetime) -> tuple[str, datetime] | None:
    if "提醒我" not in text:
        return None
    match = re.search(r"(今天|明天|后天)\s*([0-9零〇一二两三四五六七八九十百]+)\s*(?:点|时)\s*([0-9零〇一二两三四五六七八九十百]*)\s*分?", text)
    if match is None:
        return None
    hour = _parse_number(match.group(2))
    minute = _parse_number(match.group(3) or "零")
    if hour > 23 or minute > 59:
        return None
    local_now = now.astimezone(_SHANGHAI)
    offset = {"今天": 0, "明天": 1, "后天": 2}[match.group(1)]
    scheduled_date = local_now.date() + timedelta(days=offset)
    scheduled = datetime.combine(scheduled_date, time(hour, minute), tzinfo=_SHANGHAI)
    if scheduled <= local_now:
        return None
    content = text[text.index("提醒我") + 3 :].strip()
    return content, scheduled


def _parse_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value in {"", "零", "〇"}:
        return 0
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (_DIGITS.get(left, 1) * 10) + (_DIGITS.get(right, 0) if right else 0)
    result = 0
    for char in value:
        if char not in _DIGITS:
            raise ValueError(f"unsupported Chinese number: {value}")
        result = result * 10 + _DIGITS[char]
    return result
