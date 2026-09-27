"""Hybrid retrieval over the Institutional Memory archive. This is the RAG layer.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

Every memo is indexed two ways. BM25 catches exact language such as tickers,
catalysts and metric names. Latent semantic vectors (TF IDF followed by a
truncated SVD) catch paraphrase, so "cash burn ahead of a financing" finds
"runway risk and a likely raise". The two rankings are fused with reciprocal
rank fusion, the standard way to merge rankers without tuning weights.

Retrieval is point in time. A decision dated t only sees memos written before
t, and only sees an outcome if that outcome was knowable before t. The judge
prompt therefore never contains a fact the committee could not have known,
which is the same look ahead discipline a real investment firm needs before
it trusts any backtest.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass

import numpy as np

from firm import memory_archive as archive

RRF_K = 60.0
BM25_K1 = 1.4
BM25_B = 0.75
SAME_TICKER_BONUS = 0.5 / (RRF_K + 1.0)   # half a first place vote for the name's own history
DENSE_DIM = 160

_TOKEN_RE = re.compile(r"[a-z][a-z0-9.&]*[a-z0-9]|[a-z]|\d+(?:\.\d+)?%?")
_STOP = frozenset("""a an the and or of to in on for with at by from as is are was were be been being it its
this that these those than then so such but not no nor if into over under about after before between
while which who whom whose what when where why how all any each both few more most other some own same
very can will just should now also only may might would could has have had do does did per via up down
out off again further once here there""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True)
class Hit:
    memo: archive.Memo
    score: float
    bm25_rank: int
    dense_rank: int
    outcome_visible: bool

    def public(self) -> dict:
        d = self.memo.public(with_outcome=self.outcome_visible)
        d.update(score=round(self.score, 5), bm25_rank=self.bm25_rank, dense_rank=self.dense_rank,
                 outcome_visible=self.outcome_visible)
        if not self.outcome_visible:
            d["outcome"] = {"label": "not yet knowable", "status": "hidden_point_in_time"}
        return d


class MemoryIndex:
    """BM25 plus LSA over one immutable snapshot of the archive."""

    def __init__(self, memos: tuple[archive.Memo, ...]):
        self.memos = memos
        self.ids = [m.id for m in memos]
        self.asof = np.array([m.asof for m in memos], dtype=object)
        texts = [m.text() for m in memos]
        self._build_bm25(texts)
        self._build_dense(texts)

    # BM25 ---------------------------------------------------------------
    def _build_bm25(self, texts: list[str]) -> None:
        self.doc_len = np.zeros(len(texts), dtype=np.float64)
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, text in enumerate(texts):
            counts = Counter(tokenize(text))
            self.doc_len[i] = sum(counts.values())
            for term, tf in counts.items():
                self.postings.setdefault(term, []).append((i, tf))
        n = max(len(texts), 1)
        self.avg_len = float(self.doc_len.mean()) if len(texts) else 1.0
        self.idf = {t: math.log(1.0 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def bm25(self, query: str) -> np.ndarray:
        scores = np.zeros(len(self.memos), dtype=np.float64)
        for term in set(tokenize(query)):
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf[term]
            for i, tf in plist:
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * self.doc_len[i] / self.avg_len)
                scores[i] += idf * tf * (BM25_K1 + 1) / denom
        return scores

    # Dense (latent semantic) ---------------------------------------------
    def _build_dense(self, texts: list[str]) -> None:
        self.vectorizer = None
        self.svd = None
        self.dense = None
        if len(texts) < 10:
            return
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(sublinear_tf=True, min_df=2, max_df=0.5, max_features=40000,
                                          ngram_range=(1, 2), stop_words="english", dtype=np.float32)
        x = self.vectorizer.fit_transform(texts)
        dim = max(2, min(DENSE_DIM, x.shape[1] - 1, len(texts) - 1))
        self.svd = TruncatedSVD(n_components=dim, random_state=7)
        z = self.svd.fit_transform(x)
        norms = np.linalg.norm(z, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.dense = z / norms

    def dense_scores(self, query: str) -> np.ndarray:
        if self.dense is None:
            return np.zeros(len(self.memos))
        q = self.svd.transform(self.vectorizer.transform([query]))[0]
        nq = np.linalg.norm(q)
        return self.dense @ (q / nq) if nq else np.zeros(len(self.memos))

    # Search ---------------------------------------------------------------
    def search(self, query: str, *, asof: str | None = None, ticker: str | None = None, k: int = 8,
               per_ticker: int = 3, require_outcome: bool = False,
               exclude_ids: frozenset[str] | set[str] = frozenset()) -> list[Hit]:
        """Top k precedents for query, as seen from date asof (exclusive).

        require_outcome keeps only memos whose 20 day outcome was knowable before
        asof. That is the mode the evaluation and the precedent signal use.
        """
        if not self.memos:
            return []
        eligible = np.ones(len(self.memos), dtype=bool)
        if asof:
            eligible &= self.asof < asof
        if exclude_ids:
            eligible &= np.array([i not in exclude_ids for i in self.ids])
        if require_outcome:
            gate = asof or "9999-12-31"
            eligible &= np.array([m.outcome_known_before(gate) for m in self.memos])
        idx = np.flatnonzero(eligible)
        if idx.size == 0:
            return []
        bm = self.bm25(query)[idx]
        de = self.dense_scores(query)[idx]
        bm_rank = np.empty(idx.size, dtype=int)
        bm_rank[np.argsort(-bm, kind="stable")] = np.arange(1, idx.size + 1)
        de_rank = np.empty(idx.size, dtype=int)
        de_rank[np.argsort(-de, kind="stable")] = np.arange(1, idx.size + 1)
        fused = 1.0 / (RRF_K + bm_rank) + 1.0 / (RRF_K + de_rank)
        # A memo with zero lexical overlap AND no semantic similarity is noise.
        fused[(bm <= 0) & (de <= 0.05)] = 0.0
        t = (ticker or "").upper()
        if t:
            fused += np.array([SAME_TICKER_BONUS if self.memos[i].ticker == t else 0.0 for i in idx])
        order = np.argsort(-fused, kind="stable")
        gate = asof or "9999-12-31"
        hits: list[Hit] = []
        per: Counter = Counter()
        for j in order:
            if fused[j] <= 0 or len(hits) >= k:
                break
            memo = self.memos[idx[j]]
            cap = per_ticker + (1 if memo.ticker == t else 0)
            if per[memo.ticker] >= cap:
                continue
            per[memo.ticker] += 1
            hits.append(Hit(memo=memo, score=float(fused[j]), bm25_rank=int(bm_rank[j]),
                            dense_rank=int(de_rank[j]), outcome_visible=memo.outcome_known_before(gate)))
        return hits


_index_lock = threading.Lock()
_index: dict = {"key": None, "index": None}


def get_index() -> MemoryIndex:
    """Process wide index, rebuilt only when the archive file changes."""
    memos = archive.load_memos()
    key = (len(memos), memos[-1].id if memos else "")
    with _index_lock:
        if _index["key"] != key or _index["index"] is None:
            _index["index"] = MemoryIndex(memos)
            _index["key"] = key
        return _index["index"]


def search(query: str, **kwargs) -> list[Hit]:
    return get_index().search(query, **kwargs)


def precedent_signal(hits: list[Hit], direction: int) -> dict:
    """What happened after similar past setups, aligned to the call being made.

    avg_excess_20d is the similarity weighted average 20 day return versus SPY
    that followed the retrieved precedents. aligned flips the sign for a SELL,
    so a positive aligned number always means the memory supports the call.
    """
    scored = [h for h in hits if h.outcome_visible and h.memo.excess_20d is not None]
    if not scored:
        return {"n": 0, "avg_excess_20d": None, "aligned": None, "stance": "no memory"}
    w = np.array([h.score for h in scored])
    w = w / w.sum() if w.sum() > 0 else np.full(len(scored), 1.0 / len(scored))
    ex = np.array([h.memo.excess_20d for h in scored])
    avg = float((w * ex).sum())
    directional = [h for h in scored if h.memo.direction != 0]
    right = sum(1 for h in directional if h.memo.label == "right")
    aligned = avg * direction if direction else None
    stance = "neutral"
    if aligned is not None:
        stance = "supports" if aligned > 0 else "warns"
    return {"n": len(scored), "avg_excess_20d": round(avg, 2),
            "aligned": None if aligned is None else round(aligned, 2),
            "precedent_calls": len(directional), "precedent_calls_right": right, "stance": stance}


def _outcome_phrase(h: Hit) -> str:
    m = h.memo
    if not h.outcome_visible:
        return "outcome not yet knowable at this date"
    ex = m.excess_20d
    sign = "+" if ex >= 0 else "minus "
    verdict = {"right": "the call was RIGHT", "wrong": "the call was WRONG"}.get(m.label, "no directional call")
    return f"next 20 trading days vs SPY {sign}{abs(ex):.1f}%, {verdict}"


def precedent_block(query: str, *, ticker: str, asof: str, direction: int = 0,
                    k: int = 6) -> tuple[str, list[Hit], dict]:
    """The INSTITUTIONAL MEMORY section injected into committee prompts.

    Returns the prompt text, the hits behind it (for the audit trail and the
    guardrails) and the precedent signal.
    """
    hits = search(query, asof=asof, ticker=ticker, k=k)
    signal = precedent_signal(hits, direction)
    if not hits:
        return "", hits, signal
    lines = ["INSTITUTIONAL MEMORY. Retrieved precedents from this firm's own past committee "
             f"decisions, point in time as of {asof}. Outcomes appear only where they were "
             "already knowable. When a precedent informs your view, cite it inline as [P#]. "
             "Never cite a precedent you did not use, and treat precedents as evidence about "
             "similar past calls, not as guarantees."]
    for i, h in enumerate(hits, 1):
        m = h.memo
        conv = f" conviction {m.conviction}" if m.conviction is not None else ""
        lines.append(f"[P{i}] {m.asof} {m.ticker} {m.rating}{conv}. {m.headline(200)} "
                     f"Result, {_outcome_phrase(h)}.")
    if signal["n"]:
        lines.append(f"Memory signal. {signal['n']} precedents with known outcomes averaged "
                     f"{signal['avg_excess_20d']:+.1f}% versus SPY over the following 20 trading days.")
    return "\n".join(lines) + "\n\n", hits, signal


# Firm playbook ----------------------------------------------------------------
# The second kind of institutional memory. Precedents record WHAT the firm
# decided and what happened. The playbook records HOW the firm analyzes, the
# research methodology (financial statements, thesis work, comps, DCF, risk
# policy) plus the lessons the firm distilled from its own graded trades. For a
# firm with decades of research this is the house view, the style guide and the
# valuation manual. Here it is the author's own certification notes, rewritten.

PLAYBOOK_DIR = archive.MEMORY_DIR / "playbook"


@dataclass(frozen=True)
class Passage:
    id: str
    chapter: str
    section: str
    source: str
    text: str

    def public(self) -> dict:
        return {"id": self.id, "chapter": self.chapter, "section": self.section,
                "source": self.source, "text": self.text}


def _split_markdown(path) -> list[Passage]:
    lines = path.read_text(encoding="utf-8").splitlines()
    chapter, source, out = path.stem, "", []
    section, buf = None, []

    def flush():
        if section and buf:
            body = " ".join(" ".join(buf).split())
            out.append(Passage(id=f"{path.stem}#{len(out) + 1}", chapter=chapter,
                               section=section, source=source, text=body))

    for line in lines:
        if line.startswith("# "):
            chapter = line[2:].strip()
        elif line.startswith("## "):
            flush()
            section, buf = line[3:].strip(), []
        elif section is None and line.strip() and not source:
            source = line.strip()
        elif section is not None:
            buf.append(line.strip())
    flush()
    return out


def _lesson_passages() -> list[Passage]:
    text = archive.lessons_text()
    out = []
    for block in re.split(r"\n(?=\d+\.\s)", text):
        m = re.match(r"(\d+)\.\s+(.*)", block.strip(), re.DOTALL)
        if not m:
            continue
        body = " ".join(m.group(2).split())
        out.append(Passage(id=f"lessons#{m.group(1)}", chapter="Firm lessons distilled from graded trades",
                           section=f"Lesson {m.group(1)}", source="Loch Raven learning review, 2026-09-18",
                           text=body))
    return out


def load_playbook() -> tuple[Passage, ...]:
    passages: list[Passage] = []
    if PLAYBOOK_DIR.is_dir():
        for p in sorted(PLAYBOOK_DIR.glob("*.md")):
            passages.extend(_split_markdown(p))
    passages.extend(_lesson_passages())
    return tuple(passages)


class KnowledgeIndex:
    """BM25 plus TF IDF cosine over playbook passages, fused by reciprocal rank."""

    def __init__(self, passages: tuple[Passage, ...]):
        self.passages = passages
        texts = [f"{p.chapter} {p.section} {p.section} {p.text}" for p in passages]
        self.doc_len = np.array([len(tokenize(t)) for t in texts], dtype=np.float64)
        self.avg_len = float(self.doc_len.mean()) if len(texts) else 1.0
        self.tf = [Counter(tokenize(t)) for t in texts]
        df = Counter(term for c in self.tf for term in c)
        n = max(len(texts), 1)
        self.idf = {t: math.log(1.0 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.vectorizer = None
        if len(texts) >= 3:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.vectorizer = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), stop_words="english")
            self.matrix = self.vectorizer.fit_transform(texts)

    def search(self, query: str, k: int = 3) -> list[tuple[Passage, float]]:
        if not self.passages:
            return []
        q = set(tokenize(query))
        bm = np.zeros(len(self.passages))
        for i, tf in enumerate(self.tf):
            for term in q & tf.keys():
                f = tf[term]
                bm[i] += self.idf[term] * f * (BM25_K1 + 1) / (
                    f + BM25_K1 * (1 - BM25_B + BM25_B * self.doc_len[i] / self.avg_len))
        if self.vectorizer is not None:
            cos = (self.matrix @ self.vectorizer.transform([query]).T).toarray().ravel()
        else:
            cos = np.zeros(len(self.passages))
        r_bm = np.empty(len(bm), dtype=int)
        r_bm[np.argsort(-bm, kind="stable")] = np.arange(1, len(bm) + 1)
        r_cos = np.empty(len(cos), dtype=int)
        r_cos[np.argsort(-cos, kind="stable")] = np.arange(1, len(cos) + 1)
        fused = 1.0 / (RRF_K + r_bm) + 1.0 / (RRF_K + r_cos)
        fused[(bm <= 0) & (cos <= 0.02)] = 0.0
        order = [i for i in np.argsort(-fused, kind="stable") if fused[i] > 0][:k]
        return [(self.passages[i], float(fused[i])) for i in order]


_kindex: dict = {"key": None, "index": None}


def get_knowledge_index() -> KnowledgeIndex:
    passages = load_playbook()
    key = tuple(p.id for p in passages)
    with _index_lock:
        if _kindex["key"] != key or _kindex["index"] is None:
            _kindex["index"] = KnowledgeIndex(passages)
            _kindex["key"] = key
        return _kindex["index"]


def knowledge_block(query: str, k: int = 3) -> tuple[str, list[Passage]]:
    """The FIRM PLAYBOOK section of a prompt, passages cited as [K#]."""
    found = [p for p, _ in get_knowledge_index().search(query, k=k)]
    if not found:
        return "", []
    lines = ["FIRM PLAYBOOK. Retrieved passages from this firm's research methodology and its "
             "distilled lessons. When a passage shapes your analysis, cite it inline as [K#]."]
    for i, p in enumerate(found, 1):
        body = p.text if len(p.text) <= 900 else p.text[:897].rstrip() + "..."
        lines.append(f"[K{i}] {p.chapter}, {p.section}. {body}")
    return "\n".join(lines) + "\n\n", found


def query_from_research(packet: dict, research: dict | None) -> str:
    """Build a retrieval query from a live data packet and the desk reports."""
    parts: list[str] = [str(packet.get("ticker") or "")]
    for key in ("name", "sector", "industry", "kind"):
        v = packet.get(key)
        if isinstance(v, str):
            parts.append(v)
    for desk in (research or {}).values():
        if isinstance(desk, dict):
            for key in ("thesis", "summary", "view", "key_points", "risks", "rationale"):
                v = desk.get(key)
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, list):
                    parts.extend(str(x) for x in v[:6])
    return " ".join(p for p in parts if p)[:6000]
