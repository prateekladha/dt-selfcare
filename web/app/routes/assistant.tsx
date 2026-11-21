import { useRef, useState } from "react";
import { ArrowUp, BrainCircuit, Check, ChevronDown, Loader2, ShieldCheck, Sparkles } from "lucide-react";
import { AgentTraceView } from "~/components/AgentTrace";
import { CiteMap, RichText, SourceList } from "~/components/RichText";
import { Card } from "~/components/ui";
import type { AgentTrace, ToolSource } from "~/lib/types";
import { useSnapshot } from "./_app";

export const meta = () => [{ title: "Assistant · Self-care" }];

type Step = { label: string; done: boolean; ok?: boolean };
type Turn = { q: string; text: string; steps: Step[]; status?: string; citations: ToolSource[]; trace?: AgentTrace; error?: string; repairing?: boolean };

const TOOL_TITLES: Record<string, (a: Record<string, unknown>) => string> = {
  list_bills: () => "Your recent bills",
  get_bill: (a) => `Your ${month(a.cycle)} bill`,
  compare_bills: (a) => `How ${month(a.cycle)} compares with the month before`,
  get_current_usage: () => "Your usage this month (from your call records)",
  search_knowledge: () => "Your records and the tariff rules",
  get_cdr_evidence: (a) => `Your raw usage records for ${month(a.cycle)}`,
  get_payments: () => "Your payments",
  get_alerts: () => "Your data alerts",
  simulate_usage: () => "Scenario priced on your plan",
};
function month(c: unknown) {
  return typeof c === "string" && /^\d{4}-\d{2}$/.test(c) ? new Date(`${c}-01T00:00:00Z`).toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" }) : "";
}
function detailOf(body: string) {
  try {
    const o = JSON.parse(body.replace(/…"\(truncated\)"$/, "}"));
    delete o.source_label; delete o.source;
    return Object.entries(o).slice(0, 6).map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.slice(0, 4).join("; ") : typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
  } catch { return body.slice(0, 300); }
}

function Answer({ t }: { t: Turn }) {
  const cites = new CiteMap();
  const [open, setOpen] = useState(false);
  const [hl, setHl] = useState<number | null>(null);
  const onCite = (n: number) => { setOpen(true); setHl(n); setTimeout(() => document.getElementById(`cite-${n}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 60); };
  const body = <RichText text={t.text} cites={cites} onCite={t.status ? onCite : undefined} />;
  const byLabel = Object.fromEntries(t.citations.map((c) => [c.label, c]));
  const items = cites.labels.filter((l) => byLabel[l]).map((l) => ({ n: cites.num(l), title: (TOOL_TITLES[byLabel[l].tool] ?? (() => byLabel[l].tool))(byLabel[l].args), detail: detailOf(byLabel[l].body) }));
  const ok = t.status === "VERIFIED" || t.status === "REPAIRED";
  return (
    <>
      <div className="px-6 py-5">
        {t.steps.length > 0 && (
          <ul className="mb-4 space-y-1.5">
            {t.steps.map((s, i) => (
              <li key={i} className="flex items-center gap-2 text-[13px] text-[var(--ink-3)]">
                {s.done ? <Check size={14} className={s.ok === false ? "text-[var(--warn)]" : "text-[var(--good)]"} /> : <Loader2 size={14} className="animate-spin" />}
                {s.label}{!s.done && "…"}
              </li>
            ))}
          </ul>
        )}
        {t.error ? <p className="text-[15px] text-[var(--bad)]">{t.error}</p>
          : t.text ? body
          : <p className="flex items-center gap-2 text-[14px] text-[var(--ink-3)]"><Loader2 size={15} className="animate-spin" /> Thinking…</p>}
        {t.repairing && <p className="mt-3 text-xs text-[var(--warn)]">Double-checking a number against your records…</p>}
        {t.status && (
          <p className="mt-4 flex items-center gap-1.5 text-xs text-[var(--ink-3)]">
            <ShieldCheck size={13} className={ok ? "text-[var(--good)]" : "text-[var(--warn)]"} />
            {ok ? (t.status === "REPAIRED" ? "Checked against your records (corrected once)" : "Checked against your records") : "Couldn't be fully checked"}
            {items.length > 0 && <> · tap a number to see its source</>}
          </p>
        )}
      </div>
      {t.status && (items.length > 0 || t.trace) && (
        <div className="border-t border-[var(--line)]">
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
                  className="flex w-full items-center justify-between px-6 py-3.5 text-left text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--surface-2)]">
            <span>Sources ({items.length})</span>
            <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
          {open && (
            <div className="space-y-4 px-6 pb-6">
              <SourceList items={items} highlight={hl} />
              {t.trace && (
                <details className="rounded-lg border border-[var(--line)] px-4 py-3">
                  <summary className="flex cursor-pointer items-center gap-1.5 text-sm font-semibold"><BrainCircuit size={15} /> How this answer was produced</summary>
                  <div className="mt-3"><AgentTraceView trace={t.trace} /></div>
                </details>
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}

export default function Assistant() {
  const { home } = useSnapshot();
  const last = home.last_bill?.cycle;
  const m = last ? month(last).split(" ")[0] : "last";
  const suggestions = [
    `Why did my ${m} bill change?`,
    "Will my data last until the end of the month?",
    "What would two weeks in the US cost me?",
    "Can you prove my last bill from my usage records?",
    "Have I paid my latest bill?",
  ];
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const patch = (fn: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, i) => (i === ts.length - 1 ? fn(t) : t)));

  async function ask(question: string) {
    if (question.trim().length < 3 || busy) return;
    setBusy(true); setQ("");
    setTurns((ts) => [...ts, { q: question, text: "", steps: [], citations: [] }]);
    setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 30);
    try {
      const res = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, lang: "en" }) });
      if (!res.ok || !res.body) { const j = await res.json().catch(() => ({})); patch((t) => ({ ...t, error: j.detail ?? "The assistant is unavailable right now." })); return; }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5));
          if (ev.type === "token") patch((t) => ({ ...t, text: t.text + ev.text }));
          else if (ev.type === "reset") patch((t) => ({ ...t, text: "" }));
          else if (ev.type === "step") patch((t) => ({ ...t, steps: [...t.steps, { label: ev.label, done: false }] }));
          else if (ev.type === "step_done") patch((t) => ({ ...t, steps: t.steps.map((s, i) => (i === t.steps.length - 1 ? { ...s, done: true, ok: ev.ok } : s)) }));
          else if (ev.type === "repairing") patch((t) => ({ ...t, repairing: true }));
          else if (ev.type === "replace") patch((t) => ({ ...t, text: ev.text }));
          else if (ev.type === "done") patch((t) => ({ ...t, text: ev.answer, status: ev.status, citations: ev.citations ?? [], trace: ev.trace, repairing: false }));
        }
      }
    } catch {
      patch((t) => ({ ...t, error: "The connection dropped. Please try again." }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-[820px] space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold leading-tight">Ask about your bill</h1>
        <p className="mt-2 text-[15px] leading-relaxed text-[var(--ink-3)]">Answers come from your own bills, usage records, payments and the tariff rules. Each one shows its sources.</p>
      </div>

      {turns.length === 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {suggestions.map((s) => (
            <button key={s} onClick={() => ask(s)} className="flex items-start gap-3 rounded-[var(--radius)] border border-[var(--line)] bg-[var(--surface)] px-4 py-3.5 text-left text-[14.5px] font-medium leading-snug hover:border-[var(--ink)]">
              <Sparkles size={16} className="mt-0.5 shrink-0 text-[var(--brand)]" /><span>{s}</span>
            </button>
          ))}
        </div>
      )}

      <div className="space-y-6">
        {turns.map((t, i) => (
          <div key={i} className="space-y-3">
            <div className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md bg-[var(--ink)] px-4 py-2.5 text-[15px] leading-relaxed text-[var(--surface)]">{t.q}</div>
            <Card className="overflow-hidden"><Answer t={t} /></Card>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <form onSubmit={(e) => { e.preventDefault(); ask(q); }}
            className="sticky bottom-20 flex items-center gap-2 rounded-[var(--radius)] border border-[var(--line-strong)] bg-[var(--surface)] p-2 shadow-[var(--shadow-lg)] md:bottom-6">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about your bills, usage or plan" aria-label="Your question"
               className="h-11 min-w-0 flex-1 bg-transparent px-3 text-[15px] outline-none" maxLength={400} />
        <button disabled={busy || q.trim().length < 3} aria-label="Ask" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-[var(--brand)] text-white disabled:opacity-40">
          {busy ? <Loader2 size={18} className="animate-spin" /> : <ArrowUp size={18} />}
        </button>
      </form>
    </div>
  );
}
