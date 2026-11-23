import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router";
import { ArrowLeft, ArrowUp, BrainCircuit, Check, ChevronDown, History, Loader2, MessageSquare, Plus, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { AgentTraceView } from "~/components/AgentTrace";
import { OrderCard, PaymentCard } from "~/components/OrderCard";
import { CiteMap, RichText, SourceList } from "~/components/RichText";
import { cycleLabel } from "~/lib/format";
import type { AgentTrace, ToolSource } from "~/lib/types";
import { useDegraded, useSnapshot } from "~/routes/_app";

/* The assistant is an action available on every page, not a destination: a launcher button opens a panel
 * (docked on the right on desktop, full screen on phones) that stays mounted while the customer moves
 * around the app, so a streaming answer, a half-typed question or an approval card survives navigation.
 * The open conversation is in the URL as ?assistant=<id> (reload, bookmark and shared links reopen it). */

type Step = { label: string; done: boolean; ok?: boolean };
type Proposal = { kind?: string; order_id: string; add_on: string; extra_data: string; price: string; purpose?: "BILL" | "ADVANCE"; bill_id?: string | null; cycle?: string; overdue?: boolean };
type Turn = { q: string; text: string; steps: Step[]; proposals?: Proposal[]; status?: string; citations: ToolSource[]; trace?: AgentTrace; error?: string };
type ChatMeta = { id: string; title: string; updated_at: string; turns: number };

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
  get_orders: () => "Your add-on and tariff orders",
  get_plans_and_addons: () => "Tariffs and add-ons you can order",
  propose_addon: () => "The add-on prepared for you",
  propose_plan_change: () => "The tariff change prepared for you",
  propose_payment: () => "The payment prepared for you",
};
function month(c: unknown) {
  return typeof c === "string" && /^\d{4}-\d{2}$/.test(c) ? new Date(`${c}-01T00:00:00Z`).toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" }) : "";
}
/** One readable line per value: lists of records (bills, payments) become "October 2026: 41.59 EUR, overdue";
 *  nested objects become "key value" pairs. Never "[object Object]". */
function show(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (Array.isArray(v)) return v.slice(0, 4).map(show).join("; ") + (v.length > 4 ? ` (+${v.length - 4} more)` : "");
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    const head = o.month ?? o.cycle ?? o.date ?? o.paid_at ?? o.label ?? o.title;
    const amount = o.total ?? o.amount ?? o.price;
    const status = o.payment_status ?? o.status;
    if (head !== undefined && (amount !== undefined || status !== undefined))
      return `${head}: ${[amount, status].filter((x) => x !== undefined).map(String).join(", ")}`;
    return Object.entries(o).slice(0, 4).map(([k, x]) => `${k.replace(/_/g, " ")} ${typeof x === "object" ? show(x) : x}`).join(", ");
  }
  return String(v);
}
function detailOf(body: string) {
  try {
    const o = JSON.parse(body.replace(/…"\(truncated\)"$/, "}"));
    delete o.source_label; delete o.source;
    return Object.entries(o).slice(0, 6).map(([k, v]) => `${k.replace(/_/g, " ")}: ${show(v)}`).join(" · ");
  } catch { return body.slice(0, 300); }
}

/** Starter questions for the page the customer is on (the assistant itself always reads the whole account). */
function suggestionsFor(path: string, lastMonth: string): string[] {
  const bill = path.match(/^\/bills\/(\d{4}-\d{2})/);
  if (bill) {
    const m = month(bill[1]).split(" ")[0];
    return [`Why did my ${m} bill change?`, `Can you prove my ${m} bill from my usage records?`, `Have I paid my ${m} bill?`];
  }
  if (path.startsWith("/bills/current")) return ["Why is this month higher than usual?", "Will my data last until the end of the month?", "What will my next bill be?"];
  if (path.startsWith("/bills")) return ["Have I paid my latest bill?", `Why did my ${lastMonth} bill change?`, "What did I pay over the last 3 months?"];
  if (path.startsWith("/plan")) return ["Which tariff suits my usage best?", "What would I pay this month if I switch today?", "Should I buy a data add-on or change tariff?"];
  if (path.startsWith("/insights")) return ["What would two weeks in the US cost me?", "What does roaming in the EU cost on my plan?", "Will my data last until the end of the month?"];
  return [`Why did my ${lastMonth} bill change?`, "Will my data last until the end of the month?", "Have I paid my latest bill?", "What would two weeks in the US cost me?"];
}

function Answer({ t }: { t: Turn }) {
  const { home } = useSnapshot();
  const cites = new CiteMap().scan(t.text);
  const [open, setOpen] = useState(false);
  const [hl, setHl] = useState<number | null>(null);
  const onCite = (n: number) => { setOpen(true); setHl(n); setTimeout(() => document.getElementById(`cite-${n}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 60); };
  const body = <RichText text={t.text} cites={cites} onCite={t.status ? onCite : undefined} />;
  const byLabel = Object.fromEntries(t.citations.map((c) => [c.label, c]));
  const items = cites.labels.filter((l) => byLabel[l]).map((l) => ({ n: cites.num(l), title: (TOOL_TITLES[byLabel[l].tool] ?? (() => byLabel[l].tool))(byLabel[l].args), detail: detailOf(byLabel[l].body) }));
  const ok = t.status === "VERIFIED" || t.status === "REPAIRED";
  return (
    <>
      <div className="px-4 py-4">
        {t.steps.length > 0 && (
          <ul className="mb-3 space-y-1.5">
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
        {!!t.proposals?.length && (
          <div className="mt-4 space-y-3">{t.proposals.map((p) => p.kind === "PAYMENT"
            ? <PaymentCard key={p.order_id} purpose={p.purpose ?? "BILL"} cycle={p.cycle ?? ""} cycleLabel={cycleLabel(p.cycle ?? "", home.subscriber.locale)}
                           amount={p.price} overdue={!!p.overdue} />
            : <OrderCard key={p.order_id} orderId={p.order_id} title={p.add_on} extra={p.extra_data} price={`${p.price} + VAT`} kind={p.kind === "PLAN" ? "PLAN" : "ADDON"} />)}</div>
        )}
        {ok && (   // only checked answers carry the badge; apologies and error messages don't need one
          <p className="mt-3 flex items-center gap-1.5 text-xs text-[var(--ink-3)]">
            <ShieldCheck size={13} className="text-[var(--good)]" />
            Checked against your records{items.length > 0 && <> · tap a number to see its source</>}
          </p>
        )}
      </div>
      {t.status && items.length > 0 && (
        <div className="border-t border-[var(--line)]">
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
                  className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--surface-2)]">
            <span>Sources ({items.length})</span>
            <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
          {open && (
            <div className="space-y-4 px-4 pb-4">
              <SourceList items={items} highlight={hl} />
              {t.trace && (
                <details className="rounded-lg border border-[var(--line)] px-3 py-3">
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

type Ctx = { open: boolean; show: (question?: string) => void; hide: () => void };
const AssistantCtx = createContext<Ctx>({ open: false, show: () => {}, hide: () => {} });
/** Open the assistant from anywhere (optionally asking a question straight away). */
export const useAssistant = () => useContext(AssistantCtx);

const urlChat = () => (typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("assistant"));
function writeUrl(id: string | null, push = false) {
  const url = new URL(window.location.href);
  if ((url.searchParams.get("assistant") ?? null) === id) return;
  if (id) url.searchParams.set("assistant", id); else url.searchParams.delete("assistant");
  // the History API keeps React Router's state and doesn't re-run the page's loaders
  (push ? window.history.pushState : window.history.replaceState).call(window.history, window.history.state, "", url);
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const { home } = useSnapshot();
  const down = useDegraded("assistant");
  const { pathname, search } = useLocation();
  const lastMonth = home.last_bill?.cycle ? month(home.last_bill.cycle).split(" ")[0] : "last";
  const suggestions = suggestionsFor(pathname, lastMonth);

  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"chat" | "history">("chat");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  busyRef.current = busy;
  const [sessionId, setSessionId] = useState<string | null>(null);   // follow-up questions keep context
  const sessionRef = useRef<string | null>(null);
  sessionRef.current = sessionId;
  const [chats, setChats] = useState<ChatMeta[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const follow = useRef(true);

  const loadChats = useCallback(async () => {
    try { const r = await fetch("/api/chats"); if (r.ok) setChats(await r.json()); } catch { /* history is optional */ }
  }, []);
  const openChat = useCallback(async (id: string) => {
    setView("chat");
    const r = await fetch(`/api/chats?id=${encodeURIComponent(id)}`).catch(() => null);
    if (!r?.ok) { setTurns([]); setSessionId(null); return; }   // deleted, expired or not this customer's
    const s = await r.json();
    const t: Turn[] = [];
    for (const m of s.messages) {
      if (m.role === "user") t.push({ q: m.content, text: "", steps: [], citations: [] });
      else if (t.length) Object.assign(t[t.length - 1], { text: m.content, status: m.status ?? "VERIFIED", citations: m.citations ?? [], proposals: m.cards ?? [] });
    }
    follow.current = true;
    setTurns(t); setSessionId(id);
  }, []);

  // ?assistant=<id> (or =new) opens the panel on that conversation; back/forward follow it
  useEffect(() => {
    const sync = () => {
      const id = urlChat();
      if (!id) { setOpen(false); return; }
      setOpen(true);
      if (id !== "new" && id !== sessionRef.current) openChat(id);
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [openChat]);
  // React Router navigations rewrite the URL without ?assistant: put it back while the panel is open
  // (deferred: the router commits its own URL after this render)
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => writeUrl(sessionRef.current ?? "new"), 0);
    return () => clearTimeout(t);
  }, [pathname, search, open, sessionId]);
  useEffect(() => { if (open) loadChats(); }, [open, loadChats]);

  const ask = useCallback(async (question: string) => {
    if (question.trim().length < 2 || busyRef.current) return;
    setBusy(true); busyRef.current = true; setQ(""); setView("chat");
    if (window.matchMedia("(max-width: 767px)").matches) inputRef.current?.blur();   // phones: give the answer the screen
    follow.current = true;
    const patch = (fn: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, i) => (i === ts.length - 1 ? fn(t) : t)));
    setTurns((ts) => [...ts, { q: question, text: "", steps: [], citations: [] }]);
    try {
      const res = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, lang: "en", session_id: sessionRef.current ?? undefined }) });
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
          if (ev.type === "session") setSessionId(ev.session_id);
          else if (ev.type === "title") loadChats();
          else if (ev.type === "token") patch((t) => ({ ...t, text: t.text + ev.text }));
          else if (ev.type === "reset") patch((t) => ({ ...t, text: "" }));
          else if (ev.type === "step") patch((t) => ({ ...t, steps: [...t.steps, { label: ev.label, done: false }] }));
          else if (ev.type === "step_done") patch((t) => ({ ...t, steps: t.steps.map((s, i) => (i === t.steps.length - 1 ? { ...s, done: true, ok: ev.ok } : s)) }));
          else if (ev.type === "proposal") patch((t) => ({ ...t, proposals: [...(t.proposals ?? []), ev] }));
          else if (ev.type === "replace") patch((t) => ({ ...t, text: ev.text }));
          else if (ev.type === "done") {   // the answer is complete: free the input now (the chat title may still follow)
            patch((t) => ({ ...t, text: ev.answer, status: ev.status, citations: ev.citations ?? [], trace: ev.trace }));
            setBusy(false); busyRef.current = false; loadChats();
          }
        }
      }
    } catch {
      patch((t) => ({ ...t, error: "The connection dropped. Please try again." }));
    } finally {
      setBusy(false); busyRef.current = false;
    }
  }, [loadChats]);

  const show = useCallback((question?: string) => {
    setOpen(true);
    if (!urlChat()) writeUrl(sessionRef.current ?? "new", true);   // back closes the panel again
    if (question) ask(question);
    else setTimeout(() => { if (window.matchMedia("(min-width: 768px)").matches) inputRef.current?.focus(); }, 50);
  }, [ask]);
  const hide = useCallback(() => { setOpen(false); writeUrl(null); }, []);
  const newChat = () => { setTurns([]); setSessionId(null); setView("chat"); writeUrl("new"); };
  async function removeChat(id: string) {
    await fetch(`/api/chats?id=${encodeURIComponent(id)}&op=delete`, { method: "POST" }).catch(() => null);
    if (id === sessionRef.current) { setTurns([]); setSessionId(null); }
    loadChats();
  }

  // Wide screens: the page makes room for the docked panel instead of hiding under it (CSS in app.css)
  useEffect(() => {
    document.body.dataset.assistant = open ? "open" : "";
    return () => { document.body.dataset.assistant = ""; };
  }, [open]);

  // Escape closes; on phones the page underneath must not scroll behind the full-screen sheet
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    window.addEventListener("keydown", onKey);
    const phone = window.matchMedia("(max-width: 767px)").matches;
    const prev = document.body.style.overflow;
    if (phone) document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [open, hide]);

  // Follow the answer while it streams; scrolling up (the customer's own gesture) stops it, back at the end resumes.
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };
  useEffect(() => {
    const el = scrollRef.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [turns, open, view]);

  const ctx = useMemo<Ctx>(() => ({ open, show, hide }), [open, show, hide]);
  const title = chats.find((c) => c.id === sessionId)?.title;

  return (
    <AssistantCtx.Provider value={ctx}>
      {children}

      {/* Launcher: on every page, above the phone tab bar */}
      {!open && (
        <button type="button" onClick={() => show()} aria-label="Ask the assistant"
                className="assistant-fab fixed bottom-[84px] right-4 z-[55] inline-flex h-12 items-center gap-2 rounded-full bg-[var(--brand)] pl-4 pr-5 text-[15px] font-semibold text-white shadow-[var(--shadow-lg)] transition-transform hover:scale-[1.03] md:bottom-6 md:right-6">
          <Sparkles size={18} /> Ask
        </button>
      )}

      {/* Stays mounted while closed: a running answer keeps streaming in the background */}
      <div role="dialog" aria-modal="false" aria-label="Assistant" hidden={!open}
           className="assistant-panel fixed inset-0 z-[70] flex flex-col bg-[var(--bg)] md:inset-auto md:bottom-6 md:right-6 md:top-[84px] md:w-[420px] md:overflow-hidden md:rounded-[var(--radius)] md:border md:border-[var(--line)] md:shadow-[var(--shadow-lg)]">
        <div className="flex h-14 shrink-0 items-center gap-1 border-b border-[var(--line)] bg-[var(--surface)] px-2">
          {view === "history"
            ? <button type="button" onClick={() => setView("chat")} aria-label="Back to chat" className="rounded-md p-2 hover:bg-[var(--chip)]"><ArrowLeft size={18} /></button>
            : <span className="ml-1 flex h-8 w-8 items-center justify-center rounded-full bg-[var(--brand)] text-white"><Sparkles size={16} /></span>}
          <div className="ml-1 min-w-0 flex-1">
            <p className="truncate text-[15px] font-bold">{view === "history" ? "Chat history" : (title ?? "Assistant")}</p>
            {view === "chat" && <p className="truncate text-[11.5px] text-[var(--ink-3)]">Answers from your bills, usage and tariff, with sources</p>}
          </div>
          {view === "chat" && (
            <>
              <button type="button" onClick={() => { setView("history"); loadChats(); }} aria-label="Chat history" title="Chat history" className="rounded-md p-2 text-[var(--ink-2)] hover:bg-[var(--chip)]"><History size={18} /></button>
              <button type="button" onClick={newChat} disabled={busy || turns.length === 0} aria-label="New chat" title="New chat" className="rounded-md p-2 text-[var(--ink-2)] hover:bg-[var(--chip)] disabled:opacity-40"><Plus size={18} /></button>
            </>
          )}
          <button type="button" onClick={hide} aria-label="Close assistant" className="rounded-md p-2 text-[var(--ink-2)] hover:bg-[var(--chip)]"><X size={18} /></button>
        </div>

        {view === "history" ? (
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-3">
            <button type="button" onClick={newChat} className="mb-2 flex w-full items-center gap-2 rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] px-3 py-2.5 text-sm font-semibold hover:bg-[var(--chip)]"><Plus size={16} /> New chat</button>
            {chats.length === 0 && <p className="px-2 py-3 text-[13px] text-[var(--ink-3)]">No previous chats yet.</p>}
            {chats.map((c) => (
              <div key={c.id} className={`group flex items-center gap-1 rounded-lg ${c.id === sessionId ? "bg-[var(--brand-bg)]" : "hover:bg-[var(--chip)]"}`}>
                <button type="button" onClick={() => { openChat(c.id); writeUrl(c.id); }} className="flex min-w-0 flex-1 items-start gap-2 px-3 py-2.5 text-left">
                  <MessageSquare size={15} className="mt-0.5 shrink-0 text-[var(--ink-3)]" />
                  <span className="min-w-0"><span className="block truncate text-[13.5px] font-medium">{c.title}</span>
                    <span className="block text-[11.5px] text-[var(--ink-3)]">{new Date(c.updated_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })} · {c.turns} {c.turns === 1 ? "question" : "questions"}</span></span>
                </button>
                <button type="button" onClick={() => removeChat(c.id)} aria-label={`Delete ${c.title}`} className="mr-1 rounded-md p-2 text-[var(--ink-3)] hover:bg-[var(--surface)] hover:text-[var(--bad)] md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100"><Trash2 size={14} /></button>
              </div>
            ))}
            <p className="px-2 pt-3 text-[11px] leading-relaxed text-[var(--ink-3)]">Your last 50 chats are kept for 90 days.</p>
          </div>
        ) : (
          <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-3 py-4">
            {turns.length === 0 && (
              <div className="space-y-2.5">
                <p className="px-1 text-[14px] leading-relaxed text-[var(--ink-2)]">Ask anything about your bills, usage, payments or tariff. Every answer shows where its numbers come from.</p>
                {suggestions.map((s) => (
                  <button key={s} type="button" onClick={() => ask(s)} disabled={down}
                          className="flex w-full items-start gap-2.5 rounded-xl border border-[var(--line)] bg-[var(--surface)] px-3.5 py-3 text-left text-[14px] font-medium leading-snug hover:border-[var(--ink)] disabled:opacity-50">
                    <Sparkles size={15} className="mt-0.5 shrink-0 text-[var(--brand)]" /><span>{s}</span>
                  </button>
                ))}
              </div>
            )}
            {turns.map((t, i) => (
              <div key={i} className="space-y-2.5">
                <div className="ml-auto w-fit max-w-[88%] rounded-2xl rounded-br-md bg-[var(--ink)] px-3.5 py-2 text-[14.5px] leading-relaxed text-[var(--surface)]">{t.q}</div>
                <div className="min-w-0 overflow-hidden rounded-[var(--radius)] border border-[var(--line)] bg-[var(--surface)]"><Answer t={t} /></div>
              </div>
            ))}
            {down && (
              <div role="status" className="rounded-[var(--radius)] border border-[var(--warn)]/40 bg-[var(--warn)]/10 px-3.5 py-3 text-[14px]">
                The assistant is briefly unavailable. Your bills, usage and payments work as normal; please try again in a few minutes.
              </div>
            )}
          </div>
        )}

        {view === "chat" && (
          <form onSubmit={(e) => { e.preventDefault(); ask(q); }}
                className="flex shrink-0 items-center gap-2 border-t border-[var(--line)] bg-[var(--surface)] p-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
            <input ref={inputRef} disabled={down} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about your bills, usage or plan" aria-label="Your question"
                   className="h-11 min-w-0 flex-1 bg-transparent px-2 text-[16px] outline-none" maxLength={400} />
            {/* pointerdown would move focus off the field first: on phones that hides the keyboard and the tap lands elsewhere */}
            <button disabled={busy || down || q.trim().length < 2} aria-label="Ask" onPointerDown={(e) => e.preventDefault()}
                    className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-[var(--brand)] text-white disabled:opacity-40">
              {busy ? <Loader2 size={18} className="animate-spin" /> : <ArrowUp size={18} />}
            </button>
          </form>
        )}
      </div>
    </AssistantCtx.Provider>
  );
}
