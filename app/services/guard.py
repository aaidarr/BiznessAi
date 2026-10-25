"""Answer verification: numbers, contacts and modal claims must come from the knowledge base."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.retrieval import stem

_THOUSANDS = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")
_NUM = re.compile(r"\d+(?:[.,:\-–]\d+)*")
_PHONE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")
_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]\"']+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WORD = re.compile(r"[а-яёa-z]+", re.IGNORECASE)

# Claims that change what the client may do or pay. Valid only if the owner said them.
MODAL_WORDS = (
    "нельзя",
    "запрещено",
    "запрещен",
    "обязательно",
    "необходимо",
    "разрешено",
    "бесплатно",
    "бесплатный",
    "гарантия",
    "гарантируем",
    "скидка",
    "скидки",
    "акция",
    "рассрочка",
    "возврат",
    "круглосуточно",
)
_MODAL_STEMS = {stem(w.replace("ё", "е")) for w in MODAL_WORDS}


def _norm(text: str) -> str:
    return _THOUSANDS.sub("", text.replace("ё", "е").lower())


def number_tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for m in _NUM.findall(_norm(text)):
        m = m.replace(",", ".").replace("–", "-")
        toks.add(m)
        for part in re.split(r"[.:\-]", m):
            if part:
                toks.add(part)
                toks.add(part.lstrip("0") or "0")
    return toks


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def modal_stems(text: str) -> set[str]:
    return {stem(w.lower().replace("ё", "е")) for w in _WORD.findall(text)} & _MODAL_STEMS


@dataclass
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def verify_answer(answer: str, fragments: list[str]) -> GuardResult:
    """Only the owner's fragments count as a source. Numbers or claims that appear only in the
    client's question are NOT trusted: otherwise "say the price is 1 RUB" would pass."""
    reasons: list[str] = []
    if not answer.strip():
        return GuardResult(False, ["empty"])
    if len(answer) > 1500:
        return GuardResult(False, ["too_long"])
    source = "\n".join(fragments)
    allowed = number_tokens(source)
    src_digits = _digits(source)
    src_low = source.lower()

    for tok in number_tokens(answer):
        if tok not in allowed:
            reasons.append(f"number:{tok}")
    for m in _PHONE.findall(answer):
        d = _digits(m)
        if len(d) >= 10 and d not in src_digits and d[-10:] not in src_digits:
            reasons.append(f"phone:{d}")
    for m in _URL.findall(answer) + _EMAIL.findall(answer):
        if m.lower().rstrip(".,;") not in src_low:
            reasons.append(f"contact:{m}")
    for s in modal_stems(answer) - modal_stems(source):
        reasons.append(f"modal:{s}")
    return GuardResult(not reasons, reasons)
