"""Thin defensive adapter for 3x-ui first-class Clients API (3.11.x)."""
from __future__ import annotations
from copy import deepcopy
from urllib.parse import quote, urlsplit
import re
import httpx


class XuiError(RuntimeError):
    pass


def safe_panel_message(message: object) -> str:
    """Restrict panel-provided diagnostic text before saving it to SQLite."""
    if not isinstance(message, str):
        return "текст причины отсутствует"
    value = ''.join(ch if ch.isprintable() else ' ' for ch in message)[:900]
    value = re.sub(r"(?i)\b(?:https?|vless|trojan|ss|vmess)://\S+", "[URL скрыт]", value)
    value = re.sub(r"(?i)\b(?:authorization|bearer|token|password|passwd|secret|subid|privatekey|presharedkey|apikey|cookie|auth)\s*[:=]\s*[^\s,;]+", "[секрет скрыт]", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[UUID скрыт]", value)
    value = re.sub(r"(?<![\w])[A-Za-z0-9_+\-=/]{32,}(?![\w])", "[значение скрыто]", value)
    return (value[:350].strip() or "текст причины отсутствует")


# Never copy read-only structures into the update payload.
CLIENT_FIELDS = {
    "email", "subId", "id", "uuid", "password", "auth", "flow", "security", "reverse",
    "totalGB", "expiryTime", "limitIp", "limitHwid", "tgId", "enable",
    "comment", "group", "reset", "resetDay", "resetWeekday", "resetMax", "trafficReset",
    "trafficResetDay", "delayedStart", "delayedDays", "startAfterFirstUse", "adTag",
    "secret", "method", "encryption", "transport", "externalProxy", "inboundIds",
    # Fields returned by 3x-ui 3.11.x even for clients not attached to a WG inbound.
    # These are writable client settings and must survive full-record updates.
    "allowedIPs", "forwardedPorts", "keepAlive", "preSharedKey",
    "privateKey", "publicKey",
}
# IMPORTANT: this allowlist covers known 3.11.x fields. Unknown fields cause a fail-closed refusal.
# Nothing is allowed to silently disappear on update.
READ_ONLY_FIELDS = {"traffic", "createdAt", "updatedAt", "lastOnline", "externalConfigIds", "online"}
# Inbound associations are read through /get, but they are NOT part of the
# JSON client object accepted by /clients/update/:email. The API updates all
# existing associations automatically. Verify preservation after POST.
UPDATE_ASSOCIATIONS = {"inboundIds"}
# READ client record: "id" = numeric SQL PK, "uuid" = VLESS credential.
# WRITE client API: "id" = credential string. They are NOT the same field.
IDENTITY_IMMUTABLE = {"email", "subId", "uuid", "id"}


def _allowed_ips_for_write(raw: object) -> list[str] | None:
    """Convert 3x-ui's ClientRecord.allowedIPs (CSV string) to Client.allowedIPs ([]string).

    The GET endpoint serializes the database record, where allowedIPs is a
    comma-separated string. POST /clients/update unmarshals a different Go
    struct: model.Client, where allowedIPs must be a JSON array of strings.
    Return None for an empty stored value so the update omits this optional
    WireGuard-only field; omission preserves any stored address in the panel.
    This adapter must never guess or silently discard malformed values.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        result = [part.strip() for part in raw.split(",")]
        if not all(result):
            raise XuiError("Некорректный формат allowedIPs из API; обновление отменено")
        return result
    if isinstance(raw, list) and all(isinstance(item, str) and item for item in raw):
        return list(raw) if raw else None
    raise XuiError("Неожиданный тип allowedIPs из API; обновление отменено")


def update_payload(full_client: dict, changes: dict) -> dict:
    if not isinstance(full_client, dict) or not full_client.get("email"):
        raise XuiError("Некорректный ответ API о клиенте")
    unknown = set(full_client) - CLIENT_FIELDS - READ_ONLY_FIELDS
    if unknown:
        raise XuiError("В API появились неизвестные поля клиента: " + ", ".join(sorted(unknown)) + ". Операция заблокирована, требуется проверка адаптера")
    # Refuse attempted updates to unknown or read-only fields, too.
    invalid_changes = set(changes) - (CLIENT_FIELDS - UPDATE_ASSOCIATIONS - IDENTITY_IMMUTABLE)
    if invalid_changes:
        raise XuiError("Недопустимые поля изменения клиента: " + ", ".join(sorted(invalid_changes)))

    # The GET response is a persisted ClientRecord: id is an INTEGER database PK
    # and uuid is the VLESS credential. The POST /clients/update payload is the
    # Xray Client structure: id must be a STRING credential, never an integer.
    # See 3x-ui's frontend useClients.ts setEnable(): id: base.uuid.
    record_id = full_client.get("id")
    credential = full_client.get("uuid")
    if record_id is not None and (isinstance(record_id, bool) or not isinstance(record_id, (int, str))):
        raise XuiError("Неожиданный тип ID записи клиента; обновление запрещено")
    if credential is not None and not isinstance(credential, str):
        raise XuiError("Неожиданный тип UUID клиента; обновление запрещено")
    if isinstance(record_id, int) and not credential:
        # Cannot safely reconstruct the Xray credential from an SQL primary key.
        # Some protocol-specific clients might not have UUID; investigate them
        # separately instead of risking a credential reset on this panel version.
        raise XuiError("Числовой ID записи получен без UUID; требуется проверка клиента перед обновлением")
    if isinstance(record_id, str) and credential and record_id != credential:
        raise XuiError("Различаются UUID и строковый ID клиента; обновление запрещено")

    safe = deepcopy({k: v for k, v in full_client.items()
                     if k in CLIENT_FIELDS and k not in UPDATE_ASSOCIATIONS | {"id", "uuid"}})
    if credential:
        safe["id"] = credential
    elif isinstance(record_id, str) and record_id:
        safe["id"] = record_id
    # Never submit the persisted database PK or the GET-only alias `uuid`.
    safe.update(changes)
    # GET returns ClientRecord.allowedIPs as a CSV STRING, but the update
    # handler unmarshals model.Client.allowedIPs as []string. This type mismatch
    # broke block, renew and hwid_slots for *every* existing client, including
    # VLESS/Trojan clients with an empty stored WireGuard field. Normalize
    # AFTER applying changes so no caller can reintroduce the wrong JSON type.
    if "allowedIPs" in safe:
        allowed_ips = _allowed_ips_for_write(safe["allowedIPs"])
        if allowed_ips is None:
            safe.pop("allowedIPs")
        else:
            safe["allowedIPs"] = allowed_ips
    return safe


class XuiAPI:
    def __init__(self, url: str, token: str, verify: bool = True, timeout: float = 15):
        self.url = url.rstrip("/")
        self.http = httpx.AsyncClient(
            headers={"Authorization":f"Bearer {token}", "Accept":"application/json"},
            verify=verify, timeout=timeout, follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def _request(self, method: str, endpoint: str, payload=None):
        """Preserve a safe error category and redacted panel reason.

        Do not expose a panel URL (which can contain a secret base path),
        bearer token, request body, credentials or raw response.
        """
        parts = endpoint.strip('/').split('/')
        action = parts[2] if len(parts)>2 else 'api'
        try:
            result = await self.http.request(method, self.url + endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise XuiError(f'NETWORK_TIMEOUT: API {action}; результат POST может быть неизвестен') from exc
        except httpx.HTTPError as exc:
            raise XuiError(f'NETWORK_ERROR ({type(exc).__name__}): API {action}; результат POST может быть неизвестен') from exc
        try:
            parsed = result.json()
        except ValueError as exc:
            raise XuiError(f'INVALID_JSON (HTTP {result.status_code}): API {action}') from exc
        if not isinstance(parsed, dict):
            raise XuiError(f'INVALID_FORMAT (HTTP {result.status_code}): API {action}')
        if not 200 <= result.status_code < 300:
            reason = safe_panel_message(parsed.get('msg'))
            raise XuiError(f'HTTP_{result.status_code} ({action}): {reason}')
        if parsed.get('success') is not True:
            reason = safe_panel_message(parsed.get('msg'))
            raise XuiError(f'PANEL_REJECTED ({action}): {reason}')
        return parsed.get('obj')

    async def clients(self) -> list[dict]:
        obj = await self._request("GET", "/panel/api/clients/list")
        if not isinstance(obj, list):
            raise XuiError("Неожиданный формат списка клиентов")
        return obj

    async def get(self, email: str) -> dict:
        obj = await self._request("GET", "/panel/api/clients/get/"+quote(email,safe=""))
        if not isinstance(obj, dict):
            raise XuiError("Клиент не найден или API изменилось")
        # 3x-ui 3.11.x wraps the record in {client, inboundIds, externalLinks}.
        # Return the familiar flat representation to the rest of the application.
        if "client" in obj:
            if not isinstance(obj.get("client"), dict) or not isinstance(obj.get("inboundIds"), list):
                raise XuiError("Неожиданный формат подробной записи клиента")
            client = deepcopy(obj["client"])
            client["inboundIds"] = obj["inboundIds"]
            # Some versions provide aggregated traffic on the wrapper.
            if "usedTraffic" in obj and "traffic" not in client:
                client["traffic"] = {"up": int(obj["usedTraffic"] or 0), "down": 0}
            return client
        # Compatibility with pre-wrapper / fork variants, validated by probe.
        if "email" not in obj or not isinstance(obj.get("inboundIds"), list):
            raise XuiError("Неизвестный формат записи клиента")
        return obj

    async def inbounds(self) -> list[dict]:
        obj = await self._request("GET", "/panel/api/inbounds/list")
        if not isinstance(obj, list) or any(not isinstance(i, dict) for i in obj):
            raise XuiError("Неожиданный формат списка inbound")
        return obj

    async def create(self, client: dict, inbound_ids: tuple[int,...]):
        if not inbound_ids or len(set(inbound_ids)) != len(inbound_ids):
            raise XuiError("Список inbound пуст или содержит повторяющиеся ID")
        return await self._request("POST", "/panel/api/clients/add", {"client": client, "inboundIds":list(inbound_ids)})

    async def update(self, email: str, full_client: dict, changes: dict):
        return await self._request("POST", "/panel/api/clients/update/"+quote(email,safe=""),update_payload(full_client,changes))

    async def reset_traffic(self, email: str):
        return await self._request("POST", "/panel/api/clients/resetTraffic/"+quote(email,safe=""))

    async def hwids(self, email: str) -> list[dict]:
        obj=await self._request("POST", "/panel/api/clients/hwids/"+quote(email,safe=""))
        if not isinstance(obj,list):
            raise XuiError("Неожиданный ответ API HWID")
        return obj

    async def delete_hwid(self,email: str, device_id: int):
        if device_id <= 0:
            raise ValueError("HWID ID должен быть положительным")
        return await self._request("DELETE", f"/panel/api/clients/hwids/{quote(email,safe='')}/{device_id}")

    async def delete_client(self, email: str):
        """Permanent removal from every inbound; never call without admin confirmation."""
        return await self._request("POST", "/panel/api/clients/del/"+quote(email,safe=""))
