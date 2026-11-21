import { useState } from "react";
import { BrainCircuit, CheckCircle2, Database, FileSearch, Layers, Loader2, XCircle } from "lucide-react";
import type { AgentTrace, Citation, Evidence } from "~/lib/types";
import { AgentTraceView } from "./AgentTrace";

/** Answer text with [D#]/[G#] citations rendered as links to the cited documents below. */
export function CitedText({ text, onCite }: { text: string; onCite: (label: string) => void }) {
  const parts = text.split(/(\[[DG]\d{1,2}\])/g);
  return (
    <p className="whitespace-pre-line text-[16px] leading-[1.7] text-[var(--ink)]">
      {parts.map((part, i) => {
        const m = part.match(/^\[([DG]\d{1,2})\]$/);
        if (!m) return <span key={i}>{part}</span>;
        return (
          <button key={i} type="button" onClick={() => onCite(m[1])} aria-label={`Source ${m[1]}`}
                  className="mx-0.5 inline-flex h-[18px] min-w-[22px] -translate-y-1 items-center justify-center rounded bg-[var(--brand-bg)] px-1 align-baseline text-[10px] font-bold text-[var(--brand-ink)] hover:bg-[var(--brand)] hover:text-white">{m[1]}</button>
        );
      })}
    </p>
  );
}

const DOC_KIND: Record<string, string> = {
  bill: "Bill (Autonomous DB)", bill_change: "Bill comparison (rating engine)", usage_live: "Live usage (CDR totals in Valkey)",
  profile: "Plan (CRM)", alerts: "Alerts", payments: "Payments", tariff: "Tariff (product catalogue)", policy: "Policy (product catalogue)",
  cdr_evidence: "Raw CDR evidence (archive)",
};

export function CitedDocs({ citations, highlight }: { citations: Citation[]; highlight: string | null }) {
  return (
    <ol className="space-y-2">
      {citations.map((c) => (
        <li key={c.label} id={`src-${c.label}`} className={`rounded-md border px-3 py-2 transition-colors ${highlight === c.label ? "border-[var(--brand)] bg-[var(--brand-bg)]" : "border-[var(--line)]"}`}>
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="rounded bg-[var(--chip)] px-1.5 text-[11px] font-bold">{c.label}</span>
            <span className="text-[13px] font-semibold">{c.title}</span>
            <span className="text-[11px] text-[var(--ink-3)]">{DOC_KIND[c.doc_type] ?? c.doc_type}</span>
          </div>
          <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--ink-2)]">{c.body}</p>
          <p className="mt-1 font-mono text-[11px] text-[var(--ink-3)]">source: {c.source_ref}</p>
        </li>
      ))}
    </ol>
  );
}

const fmtQty = (q: number, unit: string) =>
  unit === "bytes" ? `${(q / 1e6).toLocaleString("en-GB", { maximumFractionDigits: 1 })} MB`
  : unit === "seconds" ? `${Math.floor(q / 60)} min ${q % 60}s` : q.toLocaleString("en-GB");

export function SourcesPanel({ citations, trace, cycle, open, setOpen, highlight }:
  { citations: Citation[]; trace?: AgentTrace; cycle: string; open: boolean; setOpen: (v: boolean) => void; highlight: string | null }) {
  const [ev, setEv] = useState<Evidence | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [err, setErr] = useState("");

  async function verify() {
    setState("loading");
    try {
      const r = await fetch(`/api/evidence/${cycle}`);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail ?? "evidence unavailable");
      setEv(j); setState("idle");
    } catch (e: any) { setErr(e.message); setState("error"); }
  }

  return (
    <div className="border-t border-[var(--line)]">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
              className="flex w-full items-center justify-between px-6 py-3 text-left text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--surface-2)]">
        <span className="inline-flex items-center gap-2"><FileSearch size={16} /> Sources, evidence and how this answer was made ({citations.length})</span>
        <span className="text-xs text-[var(--ink-3)]">{open ? "Hide" : "Show"}</span>
      </button>
      {open && (
        <div className="space-y-5 px-6 pb-6">
          <div>
            <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]"><Layers size={13} /> Documents the answer cites (built from your records)</p>
            <CitedDocs citations={citations} highlight={highlight} />
          </div>
          {trace && (
            <details className="rounded-lg border border-[var(--line)] p-4" open={false}>
              <summary className="flex cursor-pointer items-center gap-1.5 text-sm font-semibold"><BrainCircuit size={15} /> How this answer was made (agent trace)</summary>
              <div className="mt-3"><AgentTraceView trace={trace} /></div>
            </details>
          )}

          <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-2)] p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="flex items-center gap-1.5 text-sm font-semibold"><Database size={15} /> Verify against raw CDRs</p>
              {!ev && <button type="button" onClick={verify} disabled={state === "loading"}
                className="inline-flex h-9 items-center gap-2 rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] px-3.5 text-sm font-bold hover:bg-[var(--chip)] disabled:opacity-60">
                {state === "loading" ? <><Loader2 size={15} className="animate-spin" /> Re-reading archive…</> : "Re-sum my CDRs"}</button>}
            </div>
            <p className="mt-1 text-xs text-[var(--ink-3)]">Reads your raw usage records from the audit archive in Object Storage (written independently of the real-time pipeline), adds them up again and compares with the running totals that priced your bill.</p>
            {state === "error" && <p className="mt-3 text-sm text-[var(--bad)]">{err}</p>}
            {ev && (
              <div className="mt-4 space-y-4">
                <div className={`flex items-start gap-2 rounded-md px-3 py-2.5 text-sm font-medium ${ev.all_match ? "bg-[var(--good-bg)] text-[var(--good)]" : "bg-[var(--warn-bg)] text-[var(--warn)]"}`}>
                  {ev.all_match ? <CheckCircle2 size={17} className="mt-px shrink-0" /> : <XCircle size={17} className="mt-px shrink-0" />}
                  <span>{ev.cdr_count.toLocaleString("en-GB")} CDRs between {ev.first_event?.slice(0, 10)} and {ev.last_event?.slice(0, 10)} ·
                    {ev.all_match ? " every total matches the real-time counters" : " some totals differ (see below)"} · {ev.objects_scanned} archive objects scanned on stream partition P{ev.partition}</span>
                </div>
                {ev.note && <p className="text-xs text-[var(--ink-3)]">{ev.note}</p>}
                <div className="overflow-x-auto">
                  <table className="w-full text-[13px]">
                    <thead className="text-left text-[11px] uppercase tracking-wide text-[var(--ink-3)]"><tr><th className="py-1.5">Usage</th><th className="py-1.5 text-right">CDRs</th><th className="py-1.5 text-right">Re-summed from CDRs</th><th className="py-1.5 text-right">Real-time counter (Valkey)</th><th className="py-1.5 text-center">Match</th></tr></thead>
                    <tbody className="tabular divide-y divide-[var(--line)]">
                      {ev.reconciliation.map((r) => (
                        <tr key={r.key}><td className="py-1.5 font-semibold">{r.key}</td><td className="py-1.5 text-right">{r.cdr_records.toLocaleString("en-GB")}</td>
                          <td className="py-1.5 text-right">{fmtQty(r.from_cdrs, r.unit)}</td><td className="py-1.5 text-right">{fmtQty(r.valkey_counter, r.unit)}</td>
                          <td className="py-1.5 text-center">{r.match ? <CheckCircle2 size={15} className="inline text-[var(--good)]" /> : <XCircle size={15} className="inline text-[var(--bad)]" />}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="text-xs text-[var(--ink-3)]">The bill rounds each usage type up to the next started MB / minute (e.g. 371.04 MB is billed as 372 MB), per the tariff rules.</p>
                {ev.bill_lines.length > 0 && (
                  <p className="text-xs text-[var(--ink-3)]">Priced into bill <code className="text-[var(--ink-2)]">{ev.bill_id}</code>: {ev.bill_lines.map((l) => `${l.label} ${l.quantity.toLocaleString("en-GB")} ${l.unit}`).join(" · ")}</p>
                )}
                <details className="text-[13px]">
                  <summary className="cursor-pointer font-semibold text-[var(--ink-2)]">Sample CDRs ({ev.sample.length})</summary>
                  <div className="mt-2 overflow-x-auto">
                    <table className="w-full text-[12px]">
                      <thead className="text-left text-[11px] uppercase tracking-wide text-[var(--ink-3)]"><tr><th className="py-1">Record id</th><th className="py-1">Event time (UTC)</th><th className="py-1">Usage</th><th className="py-1 text-right">Quantity</th><th className="py-1">Archive object</th></tr></thead>
                      <tbody className="tabular divide-y divide-[var(--line)] font-mono">
                        {ev.sample.map((c) => (<tr key={c.record_id}><td className="py-1 pr-3">{c.record_id}</td><td className="py-1 pr-3">{c.event_time.slice(0, 19).replace("T", " ")}</td><td className="py-1 pr-3">{c.key}</td><td className="py-1 pr-3 text-right">{fmtQty(c.quantity, c.unit)}</td><td className="py-1 text-[var(--ink-3)]">{c.archive_object}</td></tr>))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
