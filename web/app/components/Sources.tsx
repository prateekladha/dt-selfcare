import { useState } from "react";
import { CheckCircle2, Database, Loader2, XCircle } from "lucide-react";
import type { Evidence } from "~/lib/types";

const fmtQty = (q: number, unit: string) =>
  unit === "bytes" ? `${(q / 1e6).toLocaleString("en-GB", { maximumFractionDigits: 1 })} MB`
  : unit === "seconds" ? `${Math.floor(q / 60)} min ${q % 60}s` : q.toLocaleString("en-GB");

/** Raw-CDR evidence: re-reads the archive, re-sums usage and reconciles with the real-time counters. */
export function EvidencePanel({ cycle }: { cycle: string }) {
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
  );
}
