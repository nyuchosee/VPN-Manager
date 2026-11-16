"""Environment-only config; no secrets in repository."""
from __future__ import annotations
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def parse_inbound_ids(value: str) -> tuple[int, ...]:
    """Accept a comma-separated list of unique positive inbound IDs.

    The number of inbounds is intentionally not limited to one. New inbounds
    must be created in the panel before they can be selected.
    """
    import re
    if not value or not re.fullmatch(r"\s*[1-9]\d*(?:\s*,\s*[1-9]\d*)*\s*", value):
        raise ValueError("INBOUND_IDS: укажите ID через запятую, например 1,2,3")
    values = tuple(int(part.strip()) for part in value.split(','))
    if len(set(values)) != len(values):
        raise ValueError("INBOUND_IDS: повторяющиеся идентификаторы запрещены")
    if len(values) > 64:
        raise ValueError("INBOUND_IDS: слишком много входящих подключений (максимум 64)")
    return values


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Заполни переменную {name} в /etc/vpn-bot/bot.env")
    return value


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_ids: frozenset[int]
    xui_url: str
    xui_token: str
    database_path: str
    subscription_template: str
    inbound_ids: tuple[int, ...]
    write_enabled: bool
    verify_tls: bool
    timeout: float
    panel_base_path: str = ""

    @classmethod
    def from_env(cls):
        admin_ids = frozenset(int(x.strip()) for x in required("ADMIN_IDS").split(",") if x.strip())
        if not admin_ids or any(x <= 0 for x in admin_ids):
            raise ValueError("ADMIN_IDS: перечисли положительные числовые Telegram ID")
        raw_url = required("XUI_URL").rstrip("/")
        url = urlsplit(raw_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("XUI_URL должен быть HTTPS URL без логина, пароля или параметров")
        inbound_ids = parse_inbound_ids(required("INBOUND_IDS"))
        template = required("SUBSCRIPTION_URL_TEMPLATE")
        if template.count("{sub_id}") != 1 or not template.startswith("https://"):
            raise ValueError("SUBSCRIPTION_URL_TEMPLATE: HTTPS и ровно один {sub_id}")
        return cls(
            telegram_token=required("TELEGRAM_BOT_TOKEN"), admin_ids=admin_ids,
            xui_url=raw_url, xui_token=required("XUI_API_TOKEN"),
            database_path=os.getenv("DATABASE_PATH", "/var/lib/vpn-bot/bot.sqlite3"),
            subscription_template=template, inbound_ids=inbound_ids,
            write_enabled=flag("WRITE_ENABLED"), verify_tls=not flag("INSECURE_SKIP_TLS_VERIFY"),
            timeout=float(os.getenv("HTTP_TIMEOUT", "15")),
        )
