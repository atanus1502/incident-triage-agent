"""Removes customer identifiers and credentials before text reaches Claude, Slack or the audit log.

Pattern matching catches structured values only. It can't recognise names, addresses or free-text details,
so have your security team test it on a sample of real data before relying on it.
"""
from __future__ import annotations

import re

_SECRET = re.compile(
    r"sk-ant-[\w-]+|xox[a-z]-[\w-]+|github_pat_\w+|gh[pousr]_\w+|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[\w-]+\.[\w-]+\.[\w-]+|(?i:\bbearer\s+)[\w.~+/=-]{8,}"
)
_SECRET_PAIR = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)[^\s,;&]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_ACCOUNT = re.compile(r"(?i)\b(account|acct|a/c)((?:[ _-]?(?:no|num|number|id))?\s*[:=#]?\s*)(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,34}\b")
_PHONE = re.compile(r"(?<![\w+])\+\d{10,15}\b")
# Starts at 2-6 so millisecond epoch timestamps (17...) aren't treated as card numbers.
_CARD = re.compile(r"(?<![\w-])[2-6](?:[ -]?\d){12,18}(?![\w-])")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[REDACTED-CARD]" if _luhn_valid(digits) else match.group(0)


def redact(text: str) -> str:
    text = _SECRET.sub("[REDACTED-SECRET]", text)
    text = _SECRET_PAIR.sub(r"\1\2[REDACTED-SECRET]", text)
    text = _EMAIL.sub("[REDACTED-EMAIL]", text)
    text = _SSN.sub("[REDACTED-SSN]", text)
    text = _IBAN.sub("[REDACTED-ACCOUNT]", text)
    text = _ACCOUNT.sub(r"\1\2[REDACTED-ACCOUNT]", text)
    text = _PHONE.sub("[REDACTED-PHONE]", text)
    return _CARD.sub(_card, text)
