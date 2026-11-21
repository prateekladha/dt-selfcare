import { useRef, useState } from "react";
import { ArrowUp, BrainCircuit, Loader2, ShieldCheck, Sparkles } from "lucide-react";
import { AgentTraceView } from "~/components/AgentTrace";
import { CitedDocs, CitedText } from "~/components/Sources";
import { Card } from "~/components/ui";
import type { AgentAnswer } from "~/lib/types";
import { useSnapshot } from "./_app";

export const meta = () => [{ title: "Assistant · Self-care" }];

type Turn = { q: string; a?: AgentAnswer; error?: string };

export default function Assistant() {
  const { home } = useSnapshot();
  const last = home.last_bill?.cycle;
  const month = last ? new Date(`${last}-01T00:00:00Z`).toLocaleString("en-GB", { month: "long", timeZone: "UTC" }) : "last";
  const suggestions = [
    `Why did my ${month} bill change?`,
    "How much data do I have left this month, and will it last?",
    "What would roaming in zone Z2 cost me on my plan?",
    "Show me proof from my CDRs for my last bill",
    "Have I paid my latest bill?",
  ];
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [hl, setHl] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  async function ask(question: string) {
    if (!question.trim() || busy) return;
    setBusy(true); setQ("");
    setTurns((t) => [...t, { q: question }]);
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, lang: "en" }) });
      const j = await r.json();
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? (r.ok ? { ...x, a: j } : { ...x, error: j.detail ?? "Something went wrong" }) : x)));
    } catch {
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error: "Network error" } : x)));
    } finally {
      setBusy(false);
      setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }

  return (
    <div className="mx-auto max-w-[860px] space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold">Ask about your bill</h1>
        <p className="mt-1 text-[15px] text-[var(--ink-3)]">An AI agent answers from documents built from your own records: bills, live CDR totals, alerts, payments and the tariff catalogue. Every answer cites its sources, every number is checked against them, and you can see exactly how the answer was made.</p>
      </div>

      {turns.length === 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {suggestions.map((s) => (
            <button key={s} onClick={() => ask(s)} className="rounded-lg border border-[var(--line)] bg-[var(--surface)] px-4 py-3 text-left text-sm font-medium hover:border-[var(--ink)]">
              <Sparkles size={14} className="mb-1 text-[var(--brand)]" />{s}
            </button>
          ))}
        </div>
      )}

      <div className="space-y-5">
        {turns.map((t, i) => (
          <div key={i} className="space-y-3">
            <div className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md bg-[var(--ink)] px-4 py-2.5 text-[15px] text-[var(--surface)]">{t.q}</div>
            <Card className="overflow-hidden">
              <div className="px-5 py-4">
                {!t.a && !t.error && <p className="flex items-center gap-2 text-sm text-[var(--ink-3)]"><Loader2 size={15} className="animate-spin" /> Retrieving your records, grading context, drafting and verifying…</p>}
                {t.error && <p className="text-sm text-[var(--bad)]">{t.error}</p>}
                {t.a && (
                  <>
                    <CitedText text={t.a.answer} onCite={(l) => { setHl(l); document.getElementById(`src-${l}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); }} />
                    <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-[var(--ink-3)]">
                      <ShieldCheck size={13} />
                      <span className={`font-semibold ${t.a.status === "VERIFIED" || t.a.status === "REPAIRED" ? "text-[var(--good)]" : "text-[var(--warn)]"}`}>
                        {t.a.status === "VERIFIED" ? "Verified" : t.a.status === "REPAIRED" ? "Verified after self-correction" : t.a.status === "BUDGET" ? "Budget limit reached" : "Not verified"}
                      </span>
                      <span>· {t.a.provider} · {t.a.citations.length} sources</span>
                    </p>
                  </>
                )}
              </div>
              {t.a && (
                <div className="space-y-3 border-t border-[var(--line)] bg-[var(--surface-2)] px-5 py-4">
                  {t.a.citations.length > 0 && <CitedDocs citations={t.a.citations} highlight={hl} />}
                  <details>
                    <summary className="flex cursor-pointer items-center gap-1.5 text-sm font-semibold"><BrainCircuit size={15} /> How this answer was made</summary>
                    <div className="mt-3"><AgentTraceView trace={t.a.trace} /></div>
                  </details>
                </div>
              )}
            </Card>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <form onSubmit={(e) => { e.preventDefault(); ask(q); }} className="sticky bottom-20 flex items-center gap-2 rounded-xl border border-[var(--line-strong)] bg-[var(--surface)] p-2 shadow-[var(--shadow-lg)] md:bottom-4">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask anything about your bills, usage or plan…" aria-label="Your question"
               className="h-11 flex-1 bg-transparent px-3 text-[15px] outline-none" maxLength={400} />
        <button disabled={busy || q.trim().length < 3} aria-label="Ask" className="flex h-11 w-11 items-center justify-center rounded-lg bg-[var(--brand)] text-white disabled:opacity-40">
          {busy ? <Loader2 size={18} className="animate-spin" /> : <ArrowUp size={18} />}
        </button>
      </form>
    </div>
  );
}
