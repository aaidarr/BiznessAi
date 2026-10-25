"""Lexical retrieval: Russian stemming + BM25 with a strict relevance gate."""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")
_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

# Question scaffolding that carries no topic.
STOPWORDS = frozenset(
    [
        "а",
        "в",
        "во",
        "вы",
        "вас",
        "вам",
        "ваш",
        "ваша",
        "ваше",
        "ваши",
        "и",
        "или",
        "на",
        "не",
        "но",
        "ни",
        "по",
        "при",
        "про",
        "с",
        "со",
        "у",
        "к",
        "ко",
        "о",
        "об",
        "от",
        "до",
        "из",
        "за",
        "для",
        "ли",
        "ли",
        "же",
        "бы",
        "то",
        "это",
        "эта",
        "этот",
        "эти",
        "там",
        "тут",
        "как",
        "какой",
        "какая",
        "какое",
        "какие",
        "каков",
        "где",
        "когда",
        "куда",
        "откуда",
        "зачем",
        "почему",
        "что",
        "чем",
        "кто",
        "сколько",
        "скольки",
        "есть",
        "быть",
        "будет",
        "можно",
        "нужно",
        "надо",
        "ещё",
        "еще",
        "уже",
        "только",
        "также",
        "мне",
        "мы",
        "нас",
        "нам",
        "наш",
        "я",
        "он",
        "она",
        "они",
        "его",
        "её",
        "их",
        "ты",
        "тебя",
        "вас",
        "привет",
        "здравствуйте",
        "добрый",
        "день",
        "вечер",
        "утро",
        "подскажите",
        "скажите",
        "пожалуйста",
        "пож",
        "расскажите",
        "хочу",
        "хотел",
        "хотела",
        "хотелось",
        "узнать",
        "интересует",
        "интересно",
        "подсказать",
        "вопрос",
        "даете",
        "даёте",
        "делаете",
        "имеете",
        "есть",
        "стоит",
        "стоимость",
        "цена",
        "цены",
        "цену",
        "сколько",
        "стоят",
    ]
)


# Everyday synonyms: a client says "позвонить", the owner wrote "телефон". Words inside one group
# are interchangeable for matching. Kept deliberately small and topical: a wrong synonym would let
# the bot answer a different question than the one asked.
SYNONYM_GROUPS = [
    "телефон позвонить звонок звонить номер связаться связь мобильный",
    "адрес находиться находитесь расположен расположены расположение добраться проехать локация",
    "режим график часы расписание открыт открыты открываетесь закрываетесь",
    "парковка парковаться припарковаться стоянка",
    "оплата оплатить оплачивать наличные безнал",
    "скидка скидки акция акции промокод",
]


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return _WORD.findall(text)


def stem(word: str) -> str:
    return _stemmer.stemWord(word.replace("ё", "е"))


_STOP_STEMS = frozenset(stem(w.replace("ё", "е")) for w in STOPWORDS)


def _build_synonyms() -> dict[str, tuple[str, ...]]:
    out: dict[str, set[str]] = {}
    for g in SYNONYM_GROUPS:
        stems = {stem(w) for w in g.split()}
        for st in stems:
            out.setdefault(st, set()).update(stems)
    return {k: tuple(sorted(v)) for k, v in out.items()}


SYNONYMS = _build_synonyms()


def content_stems(text: str) -> list[str]:
    out = []
    for w in tokenize(text):
        if w in STOPWORDS or stem(w) in _STOP_STEMS:
            continue
        if len(w) < 2 and not w.isdigit():
            continue
        out.append(stem(w))
    return out


@dataclass
class Hit:
    item_id: int
    title: str
    text: str
    score: float
    coverage: float
    matched: set[str] = field(default_factory=set)
    priority: int = 0  # 1 = written by the owner (FAQ / correction), 0 = scraped or uploaded text


@dataclass
class _Doc:
    item_id: int
    title: str
    text: str
    tf: dict[str, int]
    length: int
    priority: int = 0


class Index:
    K1 = 1.4
    B = 0.75

    def __init__(self, items: list[tuple]) -> None:
        self.docs: list[_Doc] = []
        df: dict[str, int] = {}
        for item_id, title, text, *rest in items:
            priority = rest[0] if rest else 0
            stems = content_stems(f"{title} {text}")
            tf: dict[str, int] = {}
            for s in stems:
                tf[s] = tf.get(s, 0) + 1
            self.docs.append(_Doc(item_id, title, text, tf, max(len(stems), 1), priority))
            for s in tf:
                df[s] = df.get(s, 0) + 1
        self.n = len(self.docs)
        self.df = df
        self.avg_len = (sum(d.length for d in self.docs) / self.n) if self.n else 1.0

    def idf(self, s: str) -> float:
        n_s = self.df.get(s, 0)
        return math.log(1 + (self.n - n_s + 0.5) / (n_s + 0.5)) if self.n else 0.0

    def search(self, query: str, k: int = 3) -> list[Hit]:
        q = list(dict.fromkeys(content_stems(query)))
        if not q or not self.n:
            return []
        max_idf = math.log(1 + (self.n + 0.5) / 0.5)

        # Unknown stems still count in the denominator at max weight: a word the
        # base has never seen must lower confidence, not be ignored.
        def weight(s: str) -> float:
            known = [self.idf(a) for a in SYNONYMS.get(s, (s,)) if a in self.df]
            return max(known) if known else max_idf

        denom = sum(weight(s) for s in q) or 1.0
        hits: list[Hit] = []
        for d in self.docs:
            score = 0.0
            matched: set[str] = set()
            w_matched = 0.0
            for s in q:
                # the best-scoring spelling of this concept that the fragment actually contains
                present = [a for a in SYNONYMS.get(s, (s,)) if d.tf.get(a)]
                if not present:
                    continue
                a = max(present, key=self.idf)
                f, idf = d.tf[a], self.idf(a)
                score += idf * (f * (self.K1 + 1)) / (f + self.K1 * (1 - self.B + self.B * d.length / self.avg_len))
                matched.add(s)
                w_matched += idf
            if matched:
                hits.append(Hit(d.item_id, d.title, d.text, score, w_matched / denom, matched, d.priority))
        hits.sort(key=lambda h: (round(h.coverage, 2), h.priority, h.score), reverse=True)
        return hits[:k]


MIN_COVERAGE = 0.6


def passes_gate(hits: list[Hit], query: str) -> list[Hit]:
    """Return fragments that are relevant enough; empty list means: do not call the model."""
    q = set(content_stems(query))
    if not q or not hits:
        return []
    good = [h for h in hits if h.coverage >= MIN_COVERAGE]
    if len(q) == 1:
        good = [h for h in good if q <= h.matched]
    return good


# Index cache per business, invalidated by Business.kb_version
_cache: dict[int, tuple[int, Index]] = {}
_cache_lock = threading.Lock()


def get_index(business_id: int, version: int, loader) -> Index:
    with _cache_lock:
        cached = _cache.get(business_id)
        if cached and cached[0] == version:
            return cached[1]
    idx = Index(loader())
    with _cache_lock:
        _cache[business_id] = (version, idx)
    return idx


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


_BREAK = re.compile(r"(?<=[.!?…])\s+|\n+")
# "г. Уфа", "ул. Ленина, д. 15", "тел. 123": a full stop after these does not end a sentence
_ABBREV = frozenset(
    [
        "г",
        "гг",
        "ул",
        "пр",
        "пр-т",
        "пл",
        "пер",
        "б-р",
        "бул",
        "наб",
        "ш",
        "д",
        "дом",
        "корп",
        "стр",
        "к",
        "кв",
        "оф",
        "эт",
        "пом",
        "мкр",
        "р-н",
        "обл",
        "респ",
        "им",
        "тел",
        "т",
        "ст",
        "п",
        "пос",
        "с",
        "дер",
        "ул-ца",
    ]
)
_LAST_WORD = re.compile(r"([A-Za-zА-Яа-яЁё\-]+)\.$")


def split_sentences(text: str) -> list[str]:
    """Sentence split that does not cut an address in half at 'г.' or 'ул.'."""
    out: list[str] = []
    start = 0
    for m in _BREAK.finditer(text):
        head = text[start : m.start()]
        w = _LAST_WORD.search(head)
        if w and "\n" not in m.group(0) and (w.group(1).lower() in _ABBREV or len(w.group(1)) == 1):
            continue  # abbreviation or an initial ("А. С. Пушкин"): keep going
        out.append(head.strip())
        start = m.end()
    out.append(text[start:].strip())
    return [x for x in out if x]


def best_sentences(text: str, query: str, limit: int = 2) -> str:
    """Verbatim excerpt: the sentences of `text` that best match the query."""
    sents = split_sentences(text)
    if not sents:
        return text.strip()
    q = set(content_stems(query))
    scored = []
    for i, s in enumerate(sents):
        st = set(content_stems(s))
        scored.append((len(q & st), -i, s))
    scored.sort(reverse=True)
    chosen = [s for sc, _, s in scored[:limit] if sc > 0] or [sents[0]]
    order = {s: i for i, s in enumerate(sents)}
    chosen.sort(key=lambda s: order[s])
    return " ".join(chosen)
