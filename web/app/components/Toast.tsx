import { useEffect, useState } from "react";
import { CheckCircle2, Info, XCircle, X } from "lucide-react";

/** One global, non-layout-shifting place for transient status ("Opening checkout…", "Payment received",
 *  errors). Components call toast(); nothing is inserted next to buttons, so CTAs never move. */
type Tone = "info" | "good" | "bad";
type Item = { id: number; tone: Tone; text: string };
const listeners = new Set<(t: Item) => void>();
let seq = 0;

export function toast(text: string, tone: Tone = "info") {
  const item = { id: ++seq, tone, text };
  listeners.forEach((l) => l(item));
}

export function Toaster() {
  const [items, setItems] = useState<Item[]>([]);
  useEffect(() => {
    const on = (t: Item) => {
      setItems((cur) => [...cur.slice(-2), t]);
      setTimeout(() => setItems((cur) => cur.filter((x) => x.id !== t.id)), t.tone === "bad" ? 9000 : 5000);
    };
    listeners.add(on);
    return () => { listeners.delete(on); };
  }, []);
  return (
    <div aria-live="polite" role="status"
         className="pointer-events-none fixed inset-x-0 bottom-20 z-[80] flex flex-col items-center gap-2 px-4 md:bottom-6 md:items-end md:px-6">
      {items.map((t) => (
        <div key={t.id} className="pointer-events-auto flex w-full max-w-[420px] items-start gap-3 rounded-[var(--radius)] border border-[var(--line-strong)] bg-[var(--surface)] px-4 py-3 text-[14px] shadow-[var(--shadow-lg)]">
          {t.tone === "good" ? <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-[var(--good)]" />
            : t.tone === "bad" ? <XCircle size={18} className="mt-0.5 shrink-0 text-[var(--bad)]" />
            : <Info size={18} className="mt-0.5 shrink-0 text-[var(--ink-3)]" />}
          <p className="min-w-0 flex-1 text-[var(--ink)]">{t.text}</p>
          <button type="button" aria-label="Dismiss" onClick={() => setItems((cur) => cur.filter((x) => x.id !== t.id))}
                  className="-m-1 rounded p-1 text-[var(--ink-3)] hover:text-[var(--ink)]"><X size={15} /></button>
        </div>
      ))}
    </div>
  );
}
