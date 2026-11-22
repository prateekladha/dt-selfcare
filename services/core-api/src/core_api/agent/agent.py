"""The billing assistant agent.

  question ──▶ L1 refresh spine (content-hash upsert, embeds only changed docs)
           ──▶ L2 hybrid retrieve (BM25 + vector + cycle ranker → RRF) ──▶ grader (cheap stage)
           ──▶ CRAG: if context is insufficient → corrective actions (widen, fetch by cycle, CDR-evidence tool)
           ──▶ generate with citations [D#]/[G#]  (L3: invariant system prefix → provider prefix cache)
           ──▶ verify (every number must appear in the CITED documents; citations must exist)
           ──▶ repair loop (≤2) with the unsupported numbers fed back   ── never a canned template
           ──▶ answer + citations + full trace (persisted)
  L4: per-stage max_tokens + daily USD caps, per-subscriber daily budget, per-doc / total context budgets.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from ..db import agent_traces, context_docs
from ..settings import Settings
from .embed import Embedder
from .llm import LLM, BudgetExceeded, LLMResult
from .retrieve import Candidate, hybrid
from .spine import global_docs, refresh_global, refresh_subscriber

log = structlog.get_logger("agent")
PROMPT_VERSION = "agent-v1"
LANGS = {"en": "English", "de": "German", "pl": "Polish"}
DOC_CHARS = 2400          # L4 item-level budget: ~600 tokens per document
CONTEXT_CHARS = 12000     # L4 total customer-context budget: ~3k tokens
_NUM = re.compile(r"\d+(?:[.,]\d{3})*(?:[.,]\d+)?")  # no space separators: "Z2 372" is two numbers
_CITE = re.compile(r"\[([DGS]\d{1,2})\]")
EVIDENCE_WORDS = {"cdr", "cdrs", "record", "records", "raw", "proof", "prove", "verify", "evidence"}

SYSTEM = """You are the billing assistant of a mobile operator. You answer the customer's question using ONLY
the numbered documents provided: shared knowledge [G#] below, and the customer's documents [D#] in the
user message (built from their billing records and call detail records).

RULES
1. Every sentence that states a fact must end with the citations it relies on, e.g. "... zone Z2 [D2][G1]."
2. Use only numbers that appear in the documents you cite. Keep amounts exactly as written (with currency).
3. Lead with the direct answer, then the main reasons, then at most one practical tip from the knowledge.
4. If the documents do not contain the answer, say what is missing instead of guessing.
5. Maximum 6 sentences. No greetings, no mention of documents, rules or that you are an AI.

SHARED KNOWLEDGE
{knowledge}"""


_SPACED = re.compile(r"\b\d{1,3}(?:[ \u00a0\u202f]\d{3})+\b")


def canon_spaced_numbers(text: str) -> str:
    """'1 676' / '1\u00a0676' (Polish, French: space as thousands separator) -> '1676', so a correct figure in a
    PL/DE answer isn't split into '1' and '676'. Answer side only ('Z2 372' has no word boundary inside 'Z2')."""
    return _SPACED.sub(lambda m: re.sub(r"[ \u00a0\u202f]", "", m.group(0)), text)


def _norm(tok: str) -> str:
    return re.sub(r"[.,]", "", tok).lstrip("0") or "0"


def numbers_in(t: str) -> set[str]:
    out = set()
    for tok in _NUM.findall(t):
        out.add(_norm(tok))
        out.add(_norm(re.split(r"[.,]", tok)[0]))
    return out


def verify(answer: str, labels: dict[str, str]) -> list[str]:
    """-> problems (empty = verified). labels maps D#/G# -> document text."""
    cited = _CITE.findall(answer)
    problems = [f"unknown citation [{c}]" for c in cited if c not in labels]
    body_numbers = [t for t in _NUM.findall(_CITE.sub("", answer)) if len(_norm(t)) > 1]
    if not cited:
        # factual claims with numbers must be cited; a conversational reply (greeting, clarifying
        # question) has nothing to cite and is fine as is
        return ["numbers without citations"] if body_numbers else []
    supported = set().union(*(numbers_in(labels[c]) for c in cited if c in labels))
    body = _CITE.sub("", answer)
    for tok in _NUM.findall(body):
        n = _norm(tok)
        if n not in supported and len(n) > 1:
            problems.append(f"number {tok.strip()} is not in the cited documents")
    return problems


@dataclass
class AgentResult:
    status: str
    answer: str
    citations: list[dict]
    trace: dict = field(default_factory=dict)
    provider: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "answer": self.answer, "citations": self.citations,
                "provider": self.provider, "trace": self.trace}


class BillingAgent:
    def __init__(self, settings: Settings, engine: Engine, valkey, state, embedder: Embedder, llm: LLM):
        self.s, self.engine, self.r, self.state, self.embedder, self.llm = settings, engine, valkey, state, embedder, llm
        self._knowledge = sorted(global_docs(), key=lambda d: d.id)  # fixed order = stable cacheable prefix

    # ---------------- stages ----------------
    def _grade(self, question: str, docs: list[Candidate], labels: list[str], trace: dict) -> tuple[set[str], bool, str]:
        listing = "\n".join(f"{lab}: ({d.doc_type}{', ' + d.cycle if d.cycle else ''}) {d.title}: {d.body[:280]}"
                            for lab, d in zip(labels, docs, strict=True))
        msgs = [{"role": "system", "content": "You grade retrieved documents for a billing question. Reply as JSON: "
                 '{"relevant": ["D1", ...], "sufficient": true|false, "missing": "<what is missing or empty>"}'},
                {"role": "user", "content": f"Question: {question}\n\nDocuments:\n{listing}"}]
        try:
            res = self.llm.chat("grade", msgs, json_mode=True)
            trace["stages"].append(res.as_trace())
            g = json.loads(res.text)
            rel = {x for x in g.get("relevant", []) if x in labels}
            return rel, bool(g.get("sufficient", bool(rel))), str(g.get("missing", ""))
        except BudgetExceeded as e:
            trace["degraded"].append(f"grader skipped: {e}")
        except Exception as e:  # noqa: BLE001 - a broken grader must not break answering
            trace["degraded"].append(f"grader failed: {type(e).__name__}")
        top = {lab for lab, d in zip(labels, docs, strict=True) if d.scores.get("rrf", 0) >= 0.025}  # RRF fallback
        return top or set(labels[:4]), len(top) >= 2, ""

    def _corrective(self, question: str, sub_id: str, target_cycle: str | None, docs: list[Candidate],
                    trace: dict) -> list[Candidate]:
        added: list[Candidate] = []
        have = {d.id for d in docs}
        # 1) widen the net
        wider, _ = hybrid(self.engine, self.embedder, question, sub_id, target_cycle, top_k=14)
        added += [d for d in wider if d.id not in have and d.id.startswith(sub_id)]
        trace["corrective"].append({"action": "widen_retrieval", "added": [d.id for d in added]})
        # 2) fetch documents of the cycle the question is about (metadata lookup)
        if target_cycle:
            with self.engine.connect() as cx:
                rows = cx.execute(select(context_docs).where(context_docs.c.subscriber_id == sub_id,
                                                             context_docs.c.cycle == target_cycle)).all()
            extra = [Candidate(r.id, r.doc_type, r.cycle, r.title, r.body, r.source_ref, r.content_hash, [])
                     for r in rows if r.id not in have | {a.id for a in added}]
            added += extra
            trace["corrective"].append({"action": "fetch_cycle_docs", "cycle": target_cycle, "added": [d.id for d in extra]})
        # 3) tool: raw-CDR evidence when the question asks for proof
        if EVIDENCE_WORDS & set(question.lower().replace("?", " ").split()) and target_cycle:
            try:
                from ..services.evidence import cdr_evidence

                ev = cdr_evidence(self.state, sub_id, target_cycle)
                body = (f"{ev['cdr_count']} CDRs between {ev['first_event']} and {ev['last_event']} were re-read from the "
                        "raw archive. Totals per usage type: " + "; ".join(
                            f"{x['key']} {x['cdr_records']} CDRs = {x['from_cdrs']:,} {x['unit']} "
                            f"({'matches' if x['match'] else 'differs from'} the running total)" for x in ev["reconciliation"]))
                added.append(Candidate(f"tool:cdr-evidence:{target_cycle}", "cdr_evidence", target_cycle,
                                       f"Raw CDR evidence for {target_cycle}", body, ev["source"], "", []))
                trace["corrective"].append({"action": "tool:cdr_evidence", "cycle": target_cycle, "cdrs": ev["cdr_count"]})
            except Exception as e:  # noqa: BLE001
                trace["corrective"].append({"action": "tool:cdr_evidence", "error": type(e).__name__})
        return added

    # ---------------- pipeline ----------------
    def ask(self, question: str, sub_id: str, lang: str = "en", cycle: str | None = None) -> AgentResult:
        t0 = time.monotonic()
        lang = lang if lang in LANGS else "en"
        trace: dict = {"prompt_version": PROMPT_VERSION, "stages": [], "corrective": [], "degraded": [], "attempts": []}

        # L1: keep the spine current (cheap when nothing changed: content-hash upsert)
        trace["spine"] = {"subscriber": refresh_subscriber(self.engine, self.state, self.embedder, sub_id),
                          "shared": refresh_global(self.engine, self.embedder)}

        # L2: hybrid retrieval + grader
        docs, meta = hybrid(self.engine, self.embedder, question, sub_id, cycle)
        cust = [d for d in docs if d.id.startswith(sub_id)]
        labels = [f"D{i}" for i in range(1, len(cust) + 1)]
        relevant, sufficient, missing = self._grade(question, cust, labels, trace) if cust else (set(), False, "")
        trace["retrieval"] = {**meta, "results": [{"label": lab, "id": d.id, "title": d.title, "type": d.doc_type,
                                                   "scores": d.scores, "relevant": lab in relevant}
                                                  for lab, d in zip(labels, cust, strict=True)],
                              "grader": {"sufficient": sufficient, "missing": missing}}
        chosen = [d for lab, d in zip(labels, cust, strict=True) if lab in relevant]

        # CRAG: corrective retrieval when the graded context is not enough
        if not sufficient or len(chosen) < 2:
            chosen += self._corrective(question, sub_id, meta.get("target_cycle"), chosen, trace)

        # L4: context budgets
        ctx, used = [], 0
        for d in chosen:
            body = d.body[:DOC_CHARS]
            if used + len(body) > CONTEXT_CHARS:
                trace["degraded"].append(f"context budget: dropped {d.id}")
                continue
            ctx.append((d, body))
            used += len(body)
        d_labels = {f"D{i}": f"{d.title}. {b}" for i, (d, b) in enumerate(ctx, start=1)}
        g_labels = {f"G{i}": f"{g.title}. {g.body}" for i, g in enumerate(self._knowledge, start=1)}
        labels_all = {**d_labels, **g_labels}
        citations_meta = {f"D{i}": d for i, (d, _) in enumerate(ctx, start=1)}
        citations_meta.update({f"G{i}": g for i, g in enumerate(self._knowledge, start=1)})

        # L3: response cache keyed by question + the exact content of the context + model/prompt version
        ck = hashlib.sha256(json.dumps([question.strip().lower(), lang, self.llm.provider, self.llm.model, PROMPT_VERSION,
                                        sorted((d.id, d.content_hash or hashlib.sha256(b.encode()).hexdigest())
                                               for d, b in ctx)]).encode()).hexdigest()
        cached = self.r.get(f"ans:{ck}")
        if cached:
            out = json.loads(cached)
            trace["cache"] = "hit (answer cache)"
            trace["latency_ms"] = int((time.monotonic() - t0) * 1000)
            return self._finish(sub_id, question, out["status"], out["answer"], out["cited"], citations_meta, trace)

        knowledge = "\n".join(f"[{lab}] {txt}" for lab, txt in g_labels.items())
        system = SYSTEM.format(knowledge=knowledge)  # invariant prefix → provider prefix cache (L3)
        user = ("Customer documents:\n" + "\n".join(f"[{lab}] {txt}" for lab, txt in d_labels.items())
                + f"\n\nQuestion: {question}\nAnswer in {LANGS[lang]}.")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        status, answer = "UNVERIFIED", ""
        try:
            res: LLMResult = self.llm.chat("generate", messages)
            trace["stages"].append(res.as_trace())
            answer = res.text
            for attempt in range(3):
                problems = verify(answer, labels_all)
                trace["attempts"].append({"attempt": attempt + 1, "verified": not problems, "problems": problems[:6]})
                if not problems:
                    status = "VERIFIED" if attempt == 0 else "REPAIRED"
                    break
                if attempt == 2:
                    break
                messages += [{"role": "assistant", "content": answer},
                             {"role": "user", "content": "Your answer has problems: " + "; ".join(problems[:6])
                              + ". Rewrite it so every number appears in the documents you cite, with citations."}]
                res = self.llm.chat("repair", messages)
                trace["stages"].append(res.as_trace())
                answer = res.text
        except BudgetExceeded as e:
            status, answer = "BUDGET", ""
            trace["degraded"].append(str(e))
        except Exception as e:  # noqa: BLE001
            status, answer = "ERROR", ""
            trace["degraded"].append(f"llm error: {type(e).__name__}: {e}"[:300])

        if status == "UNVERIFIED":
            answer = ("I couldn't produce an explanation that I can fully verify against your records. The breakdown "
                      "and evidence on this page are exact.")
        cited = sorted(set(_CITE.findall(answer)) & set(labels_all))
        if status in ("VERIFIED", "REPAIRED"):
            self.r.set(f"ans:{ck}", json.dumps({"status": status, "answer": answer, "cited": cited}), ex=24 * 3600)
        trace["latency_ms"] = int((time.monotonic() - t0) * 1000)
        return self._finish(sub_id, question, status, answer, cited, citations_meta, trace)

    def _finish(self, sub_id, question, status, answer, cited, meta, trace) -> AgentResult:
        stages = trace["stages"]
        trace["totals"] = {
            "cost_usd": round(sum(s["cost_usd"] for s in stages), 6),
            "prompt_tokens": sum(s["prompt_tokens"] for s in stages),
            "completion_tokens": sum(s["completion_tokens"] for s in stages),
            "cache_hit_tokens": sum(s["cache_hit_tokens"] for s in stages),
            "llm_calls": len(stages),
        }
        pt = trace["totals"]["prompt_tokens"]
        trace["totals"]["cache_hit_ratio"] = round(trace["totals"]["cache_hit_tokens"] / pt, 3) if pt else None
        citations = [{"label": lab, "id": meta[lab].id, "title": meta[lab].title, "doc_type": meta[lab].doc_type,
                      "cycle": meta[lab].cycle, "source_ref": meta[lab].source_ref, "body": meta[lab].body}
                     for lab in cited if lab in meta]
        try:
            with self.engine.begin() as cx:
                cx.execute(insert(agent_traces).values(
                    id=str(uuid.uuid4()), subscriber_id=sub_id, question=question[:500], status=status, answer=answer,
                    trace=json.dumps(trace, default=str), cost_usd=str(trace["totals"]["cost_usd"])))
        except Exception as e:  # noqa: BLE001
            log.warning("trace_persist_failed", error=str(e))
        return AgentResult(status, answer, citations, trace, f"{self.llm.provider}:{self.llm.model}")
