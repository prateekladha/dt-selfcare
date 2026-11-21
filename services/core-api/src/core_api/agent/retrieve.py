"""Layer 2 - hybrid retrieval: BM25 (lexical) + vector similarity (semantic) + cycle match (metadata),
fused with Reciprocal Rank Fusion. Vector search runs in Oracle 23ai (VECTOR_DISTANCE) when available,
otherwise over the portable embedding copy."""
from __future__ import annotations

import array
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import or_, select, text
from sqlalchemy.engine import Engine

from ..db import context_docs
from .embed import Embedder, cosine

_TOK = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "and", "in", "on", "for", "is", "my", "your", "was", "what", "why", "how", "did", "do", "does", "it", "this", "that", "with", "by", "be", "are", "i", "me"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], start=1)}


def tokens(t: str) -> list[str]:
    return [w for w in _TOK.findall(t.lower()) if w not in STOP]


@dataclass
class Candidate:
    id: str
    doc_type: str
    cycle: str | None
    title: str
    body: str
    source_ref: str
    content_hash: str
    embedding: list[float]
    scores: dict = field(default_factory=dict)


def load_candidates(engine: Engine, subscriber_id: str) -> list[Candidate]:
    with engine.connect() as cx:
        rows = cx.execute(select(context_docs).where(or_(context_docs.c.subscriber_id == subscriber_id,
                                                         context_docs.c.subscriber_id.is_(None)))).all()
    return [Candidate(r.id, r.doc_type, r.cycle, r.title, r.body, r.source_ref, r.content_hash,
                      json.loads(r.embedding_json)) for r in rows]


def bm25(query: str, docs: list[Candidate], k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    q = tokens(query)
    toks = {d.id: tokens(f"{d.title} {d.body}") for d in docs}
    n = len(docs) or 1
    avgdl = sum(len(t) for t in toks.values()) / n
    df = Counter(w for t in toks.values() for w in set(t))
    out = {}
    for d in docs:
        tf = Counter(toks[d.id])
        dl = len(toks[d.id]) or 1
        s = 0.0
        for w in q:
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * dl / avgdl))
        out[d.id] = s
    return out


def vector_scores(engine: Engine, embedder: Embedder, query: str, subscriber_id: str,
                  docs: list[Candidate]) -> tuple[dict[str, float], str]:
    qv = embedder.embed([query])[0]
    if engine.dialect.name == "oracle":
        with engine.connect() as cx:
            rows = cx.execute(text(
                "SELECT id, 1 - VECTOR_DISTANCE(embedding, :q, COSINE) AS sim FROM context_docs "
                "WHERE (subscriber_id = :s OR subscriber_id IS NULL) AND embedding IS NOT NULL"),
                {"q": array.array("f", qv), "s": subscriber_id}).all()
        if rows:
            return {r.id: float(r.sim) for r in rows}, "oracle-23ai VECTOR_DISTANCE(COSINE)"
    return {d.id: cosine(qv, d.embedding) for d in docs}, "in-process cosine"


def cycle_hint(query: str, explicit: str | None, known: list[str]) -> str | None:
    if explicit:
        return explicit
    q = query.lower()
    for name, m in MONTHS.items():
        if name in q:
            hits = [c for c in known if c and int(c[5:]) == m]
            return max(hits) if hits else None
    return None


def rrf(rankings: dict[str, list[str]], k: int = 60) -> dict[str, float]:
    fused: dict[str, float] = {}
    for ranked in rankings.values():
        for r, doc_id in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + r)
    return fused


def hybrid(engine: Engine, embedder: Embedder, query: str, subscriber_id: str, explicit_cycle: str | None = None,
           top_k: int = 8) -> tuple[list[Candidate], dict]:
    docs = load_candidates(engine, subscriber_id)
    by_id = {d.id: d for d in docs}
    lex = bm25(query, docs)
    vec, vec_engine = vector_scores(engine, embedder, query, subscriber_id, docs)
    target = cycle_hint(query, explicit_cycle, [d.cycle for d in docs])
    rankings = {
        "bm25": [i for i, s in sorted(lex.items(), key=lambda x: -x[1]) if s > 0],
        "vector": [i for i, _ in sorted(vec.items(), key=lambda x: -x[1]) if i in by_id],
    }
    if target:  # metadata ranker: documents about the cycle the question is about
        rankings["cycle"] = [d.id for d in sorted(docs, key=lambda d: (d.cycle != target, d.doc_type != "bill_change"))
                             if d.cycle == target]
    fused = rrf(rankings)
    ranked = sorted(fused, key=lambda i: -fused[i])[:top_k]
    for i in ranked:
        d = by_id[i]
        d.scores = {"bm25": round(lex.get(i, 0.0), 3), "vector": round(vec.get(i, 0.0), 3), "rrf": round(fused[i], 5),
                    **{f"rank_{k}": (v.index(i) + 1 if i in v else None) for k, v in rankings.items()}}
    return [by_id[i] for i in ranked], {"candidates": len(docs), "vector_engine": vec_engine, "target_cycle": target,
                                        "rankers": list(rankings)}
