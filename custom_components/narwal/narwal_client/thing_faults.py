"""Freo S ``robot_status.fault``.

The field is a small integer. It does not use the 32-bit J4 codes in
``error_codes.py`` (those belong to the Freo X Ultra). Every captured value
and the live reads so far are 0, and the handoff has no Freo S string table,
so a non-zero code stays an unknown fault plus the raw number.
"""

from __future__ import annotations

_NO_ERROR = {"en": "No error", "pl": "Brak błędu"}
_UNKNOWN = {"en": "Unknown fault ({code})", "pl": "Nieznany błąd ({code})"}


def fault_text(code: int | None, language: str = "en") -> str:
    """Readable fault text. ``language`` is a Home Assistant language code."""
    lang = "pl" if (language or "en").lower().startswith("pl") else "en"
    if not code:
        return _NO_ERROR[lang]
    return _UNKNOWN[lang].format(code=code)
