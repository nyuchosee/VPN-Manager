"""Pure, testable subscription rules."""
from __future__ import annotations
import re
import secrets
import string
import time
from datetime import datetime, timezone, timedelta

DAY_MS = 86_400_000
GIB = 1024 ** 3
USER_DAYS = (7, 30, 90)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_expiry(current_ms: int, days: int | None, now: int | None = None) -> int:
    if days is None:
        return 0
    if not isinstance(days, int) or not 1 <= days <= 36500:
        raise ValueError("Срок должен составлять 1–36500 дней")
    base = max(now if now is not None else now_ms(), current_ms if current_ms > 0 else 0)
    return base + days * DAY_MS


def replace_expiry(days: int | None, now: int | None = None) -> int:
    """New period always begins at approval, never sums remaining days."""
    if days is None:
        return 0
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 36500:
        raise ValueError("Срок должен составлять 1–36500 дней")
    return (now if now is not None else now_ms()) + days * DAY_MS


def exact_expiry(text: str, now: int | None = None) -> int:
    """Explicit UTC endpoint: YYYY-MM-DD [HH:MM]. Date only means 23:59 UTC."""
    from datetime import datetime
    text = text.strip()
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, '%Y-%m-%d').replace(hour=23, minute=59, tzinfo=timezone.utc)
        else:
            dt = datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError('Введите дату YYYY-MM-DD или YYYY-MM-DD HH:MM (UTC)') from exc
    millis = int(dt.timestamp() * 1000)
    start = now if now is not None else now_ms()
    if millis <= start:
        raise ValueError('Дата окончания должна быть в будущем')
    if millis > start + 36500 * DAY_MS:
        raise ValueError('Дата окончания слишком далека')
    return millis

def gb_bytes(gb: int | None) -> int:
    if gb is None:
        return 0
    if not isinstance(gb, int) or not 1 <= gb <= 1000000:
        raise ValueError("Количество ГБ должно быть 1–1000000")
    return gb * GIB


def validate_hwid(n: int | None) -> int:
    if n is None:
        return 0
    if not isinstance(n, int) or not 1 <= n <= 1000:
        raise ValueError("Количество устройств должно быть 1–1000")
    return n


def client_name(username: str | None, telegram_id: int, taken: set[str], alias: str | None = None) -> str:
    raw = alias if alias is not None and alias.strip() else (username or f"tg_{telegram_id}")
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", raw.lstrip("@")).strip("_.-")[:45]
    if not base:
        base = f"tg_{telegram_id}"
    if base not in taken:
        return base
    for _ in range(20):
        suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
        candidate = f"{base[:43]}_{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError("Не удалось подобрать уникальное имя")


def status(client: dict) -> str:
    exp = int(client.get("expiryTime") or 0)
    if exp < 0:  # delayed start or special values; never report active blindly
        return "Ожидает активации/особый срок"
    if exp and exp <= now_ms():
        return "Истекла"
    limit = int(client.get("totalGB") or 0)
    traffic = client.get("traffic") or {}
    used = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
    if limit and used >= limit:
        return "Трафик исчерпан"
    if client.get("enable") is False:
        return "Отключена"
    return "Активна"


def remaining(expiry_ms: int) -> str:
    if expiry_ms == 0:
        return "Бессрочно"
    if expiry_ms < 0:
        return "Особый срок (уточните у администратора)"
    delta = max(0, expiry_ms - now_ms())
    days, remainder = divmod(delta // 1000, 86400)
    hours, minutes = divmod(remainder // 60, 60)
    if days:
        return f"{days} дн. {hours} ч."
    return f"{hours} ч. {minutes} мин."


def date_str(timestamp_ms: int) -> str:
    if timestamp_ms == 0:
        return "Бессрочно"
    if timestamp_ms < 0:
        return "Особый режим"
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
