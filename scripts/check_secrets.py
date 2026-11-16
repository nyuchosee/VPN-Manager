#!/usr/bin/env python3
"""Public release preflight, including decoded self-extracting installer payloads.

This check is conservative, not a substitute for reviewing the full Git history.
"""
from __future__ import annotations

import base64
import ipaddress
import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.idea', '.mypy_cache'}
BINARY_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.pdf', '.pyc', '.woff', '.woff2'}
BLOCKED_FILENAMES = {'bot.env', '.env', 'id_rsa', 'id_ed25519', 'x-ui.db', 'bot.sqlite3'}
BLOCKED_SUFFIX = {'.pem', '.p12', '.pfx', '.key'}
TOKENS = [
    re.compile(r'\bgh[pousr]_[A-Za-z0-9_]{20,}'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{35,}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
]
IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
PLACEHOLDER_IPS = tuple(ipaddress.ip_network(x) for x in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
PAYLOAD_MARKER = b'\n__VPN_MANAGER_PAYLOAD_BELOW__\n'
problems: list[str] = []


def inspect_text(path: str, raw: bytes) -> None:
    try:
        content = raw.decode('utf-8')
    except UnicodeError:
        problems.append(f'Non-UTF-8 text: {path}')
        return
    for pattern in TOKENS:
        if pattern.search(content):
            problems.append(f'Potential token/private key: {path} ({pattern.pattern[:32]})')
    for match in IPV4.finditer(content):
        try:
            ip = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if ip.is_global and not any(ip in net for net in PLACEHOLDER_IPS):
            problems.append(f'Public IPv4 literal: {path} (line {content.count(chr(10), 0, match.start()) + 1})')


def inspect_file(path: Path, prefix: str = '') -> None:
    rel = path.relative_to(ROOT).as_posix()
    label = prefix + rel
    if path.name in BLOCKED_FILENAMES or path.suffix.lower() in BLOCKED_SUFFIX:
        problems.append(f'Secret-like filename: {label}')
        return
    if path.suffix.lower() in BINARY_EXT:
        return
    raw = path.read_bytes()
    if PAYLOAD_MARKER in raw:
        head, encoded = raw.rsplit(PAYLOAD_MARKER, 1)
        inspect_text(label + ' (header)', head)
        try:
            archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(b''.join(encoded.split()), validate=True)))
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if Path(member.filename).name in BLOCKED_FILENAMES or Path(member.filename).suffix.lower() in BLOCKED_SUFFIX:
                    problems.append(f'Secret-like embedded filename: {member.filename}')
                elif Path(member.filename).suffix.lower() not in BINARY_EXT:
                    inspect_text(label + ' -> ' + member.filename, archive.read(member))
        except (ValueError, zipfile.BadZipFile, base64.binascii.Error) as exc:
            problems.append(f'Invalid embedded archive in {label}: {type(exc).__name__}')
    else:
        inspect_text(label, raw)


for file in sorted(ROOT.rglob('*')):
    if file.is_file() and not any(part in SKIP_DIRS for part in file.relative_to(ROOT).parts):
        inspect_file(file)

if problems:
    print('PUBLIC RELEASE CHECK FAILED:')
    print('\n'.join(' - ' + issue for issue in problems))
    raise SystemExit(1)
print('Public release preflight: no detected token shapes or public IPv4 literals in files/embedded payloads.')
print('Manual review of domains, user identifiers, git history and attached artifacts is still required.')
