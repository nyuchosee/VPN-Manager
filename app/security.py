"""Input policy and bounded flood protection for Telegram-based management.

No Telegram user-controlled field is executed as SQL, shell, template, HTML or URL.
User IDs come solely from authenticated Telegram update metadata, never from text.
"""
from __future__ import annotations
from collections import deque
import re
import time
import unicodedata


def validate_alias(value: str) -> str:
    """Human-readable label, preserved in Unicode (unlike 3x-ui client email)."""
    if not isinstance(value, str):
        raise ValueError('Псевдоним должен быть текстом')
    result = unicodedata.normalize('NFKC', value).strip()
    if not 1 <= len(result) <= 48:
        raise ValueError('Длина псевдонима должна быть 1–48 символов')
    # Do not permit Unicode control/format/bidi overrides, Markdown/HTML markup,
    # newline, look-alike invisible separators or arbitrary command syntax.
    if any(not (ch.isalnum() or ch in ' _.-()') for ch in result):
        raise ValueError('Разрешены буквы, цифры, пробел, _, -, точка и скобки')
    if re.search(r'\s{3,}',result):
        raise ValueError('Слишком много пробелов подряд')
    return result


def safe_label(value: object, limit:int=50) -> str:
    """Untrusted panel/TG strings shown as plain text, without control chars."""
    text=str(value or '')
    text=''.join(ch for ch in text if unicodedata.category(ch) not in ('Cc','Cf','Cs'))
    text=text.replace('\n',' ').replace('\r',' ').strip()
    return text[:limit]


class RateGate:
    """In-memory anti-flood. Bounded per-user and global sliding windows.

    This is a resource protection, not an authentication mechanism. Restart
    resets its counters, so risky external actions must remain idempotent.
    """
    def __init__(self, per_user:int=40, admin_user:int=150, global_limit:int=900,
                 window:float=60, max_users:int=10000, clock=None):
        self.per_user=per_user
        self.admin_user=admin_user
        self.global_limit=global_limit
        self.window=window
        self.max_users=max_users
        self.clock=clock or time.monotonic
        self.events:dict[int,deque[float]]={}
        self.global_events:deque[float]=deque()
        self._calls=0

    def allow(self, uid:int, admin:bool=False) -> bool:
        now=self.clock(); threshold=now-self.window
        self._calls+=1
        if self._calls % 128 == 0:
            self.events={key:vals for key,vals in self.events.items() if vals and vals[-1]>threshold}
        if uid not in self.events:
            if len(self.events)>=self.max_users:
                # Never evict active keys to accommodate a flood of new IDs.
                self.events={key:vals for key,vals in self.events.items() if vals and vals[-1]>threshold}
                if len(self.events)>=self.max_users: return False
            self.events[uid]=deque()
        user_events=self.events[uid]
        while user_events and user_events[0]<=threshold:user_events.popleft()
        while self.global_events and self.global_events[0]<=threshold:self.global_events.popleft()
        if len(user_events)>=(self.admin_user if admin else self.per_user):return False
        if len(self.global_events)>=self.global_limit:return False
        user_events.append(now)
        self.global_events.append(now)
        return True
