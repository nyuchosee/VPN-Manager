"""Validate selected inbound IDs before creating a subscription."""
from __future__ import annotations

# Client-centric 3x-ui supports these protocols; non-client service endpoints
# (such as dokodemo-door, HTTP and SOCKS) are intentionally not permitted here.
CLIENT_PROTOCOLS = frozenset({"vless", "vmess", "trojan", "shadowsocks", "hysteria", "hysteria2", "tuic"})


def validate_selected_inbounds(selected: tuple[int, ...], available: list[dict]) -> list[dict]:
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Нужно выбрать уникальные inbound ID")
    indexed = {int(item["id"]): item for item in available if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
    missing = set(selected) - indexed.keys()
    if missing:
        raise ValueError("В 3x-ui отсутствуют inbound ID: " + ", ".join(map(str, sorted(missing))) + ". Сначала создайте их в панели, затем повторно настройте бота")
    disabled = [i for i in selected if indexed[i].get("enable") is not True]
    if disabled:
        raise ValueError("Выбранные inbound выключены: " + ", ".join(map(str, disabled)))
    unsupported = [i for i in selected if str(indexed[i].get("protocol", "")).lower() not in CLIENT_PROTOCOLS]
    if unsupported:
        raise ValueError("Неподдерживаемый тип inbound у ID: " + ", ".join(map(str, unsupported)) + ". Требуется отдельная проверка")
    return [indexed[i] for i in selected]
