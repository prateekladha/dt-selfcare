import { Suspense, useState } from "react";
import { Await, Link } from "react-router";
import { BrainCircuit, ChevronDown, Lightbulb, ShieldCheck, Sparkles, TrendingDown, TrendingUp } from "lucide-react";
import type { Explanation, ExplanationResponse, LlmItem, SourceDoc } from "~/lib/types";
import { money } from "~/lib/format";
import { AgentTraceView } from "./AgentTrace";
import { CiteMap, CiteMark, Inline, SourceList } from "./RichText";
import { EvidencePanel } from "./Sources";
import { Card, CardHeader, Pill, Skeleton } from "./ui";

export function DeltaPill({ ex, locale }: { ex: Explanation; locale: string }) {
  if (ex.delta_total === null || ex.direction === "SAME") return <Pill>No change</Pill>;
  const up = ex.direction === "UP";
  return (
    <Pill tone={up ? "bad" : "good"} icon={up ? <TrendingUp size={13} /> : <TrendingDown size={13} />}>
      {up ? "+" : "−"}{money(Math.abs(ex.delta_total), ex.currency, locale)}{ex.delta_pct !== null && ` · ${Math.abs(ex.delta_pct).toFixed(0)}%`}
    </Pill>
  );
}

const LANGS = [["en", "English"], ["de", "Deutsch"], ["pl", "Polski"]] as const;

function sourceTitle(s: SourceDoc): { title: string; detail: string } {
  const body = s.body;
  const cut = body.indexOf(": ");
  if (s.label.startsWith("G")) {
    const dot = body.indexOf(". ");
    return { title: dot > 0 ? body.slice(0, dot) : "Tariff rules", detail: dot > 0 ? body.slice(dot + 2) : body };
  }
  return cut > 0 && cut < 60 ? { title: body.slice(0, cut), detail: body.slice(cut + 2) } : { title: "Your records", detail: body };
}

function ItemCites({ item, cites, onCite }: { item: { sources?: string[] }; cites: CiteMap; onCite: (n: number) => void }) {
  return <>{(item.sources ?? []).map((l) => { const n = cites.num(l); return <CiteMark key={l} n={n} onClick={() => onCite(n)} />; })}</>;
}

function ItemRow({ item, value, tone, cites, onCite }: { item: LlmItem; value?: string; tone: "up" | "down" | "neutral"; cites: CiteMap; onCite: (n: number) => void }) {
  const color = tone === "up" ? "text-[var(--bad)]" : tone === "down" ? "text-[var(--good)]" : "text-[var(--ink)]";
  return (
    <li className="py-3.5 first:pt-0 last:pb-0">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-[15px] font-semibold text-[var(--ink)]">{item.title}</p>
        {value && <p className={`tabular shrink-0 text-[15px] font-bold ${color}`}>{value}</p>}
      </div>
      <p className="mt-1 text-[14px] leading-relaxed text-[var(--ink-2)]">
        <Inline text={item.why} cites={cites} /><ItemCites item={item} cites={cites} onCite={onCite} />
      </p>
    </li>
  );
}

function Loading() {
  return (
    <div className="grid gap-6 lg:grid-cols-5" aria-busy="true">
      <Card className="p-6 lg:col-span-3"><div className="space-y-3"><Skeleton className="h-5 w-1/3" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-4/5" /></div></Card>
      <Card className="p-6 lg:col-span-2"><div className="space-y-3"><Skeleton className="h-5 w-1/2" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-2/3" /></div></Card>
    </div>
  );
}

/** "Why your bill changed": summary, drivers, top usage and tip, all written by the LLM from computed
 * facts, each item citing numbered sources; plus evidence and the trace of how it was produced. */
export function ExplanationSection({ data, lang, basePath, cycle, heading, showLanguages = true }:
  { data: Promise<ExplanationResponse> | ExplanationResponse; lang: string; basePath: string; cycle?: string; heading: string; showLanguages?: boolean }) {
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="display text-[24px] font-extrabold leading-tight">{heading}</h2>
        {showLanguages && (
          <div className="flex rounded-lg bg-[var(--chip)] p-0.5" role="tablist" aria-label="Language">
            {LANGS.map(([k, l]) => (
              <Link key={k} to={`${basePath}?lang=${k}`} preventScrollReset replace role="tab" aria-selected={lang === k}
                    className={`rounded-md px-3 py-1.5 text-xs font-semibold ${lang === k ? "bg-[var(--surface)] text-[var(--ink)] shadow-sm" : "text-[var(--ink-3)] hover:text-[var(--ink)]"}`}>{l}</Link>
            ))}
          </div>
        )}
      </div>
      <Suspense key={lang} fallback={<Loading />}>
        <Await resolve={data} errorElement={<Unavailable />}>
          {(r) => (r.llm ? <Body r={r} cycle={cycle} /> : <Unavailable />)}
        </Await>
      </Suspense>
    </section>
  );
}

function Unavailable() {
  return (
    <Card className="px-6 py-5">
      <p className="text-[15px] text-[var(--ink-2)]">The explanation isn't available right now. Your bill breakdown above is complete and exact.</p>
    </Card>
  );
}

function Body({ r, cycle }: { r: ExplanationResponse; cycle?: string }) {
  const llm = r.llm!;
  const cites = new CiteMap();
  const [open, setOpen] = useState(false);
  const [hl, setHl] = useState<number | null>(null);
  const onCite = (n: number) => {
    setOpen(true); setHl(n);
    setTimeout(() => document.getElementById(`cite-${n}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
  };
  const byLabel = Object.fromEntries((r.sources ?? []).map((s) => [s.label, s]));
  const up = (v?: string) => (v ?? "").trim().startsWith("+");
  const down = (v?: string) => /^[−-]/.test((v ?? "").trim());

  // render once to assign citation numbers in reading order
  const summary = <Inline text={llm.summary} cites={cites} />;
  const drivers = llm.drivers.map((d, i) => <ItemRow key={i} item={d} value={d.impact} tone={up(d.impact) ? "up" : down(d.impact) ? "down" : "neutral"} cites={cites} onCite={onCite} />);
  const usage = llm.top_usage.map((d, i) => <ItemRow key={i} item={d} value={d.amount} tone="neutral" cites={cites} onCite={onCite} />);
  const tipCites = (llm.tip_sources ?? []).map((l) => { const n = cites.num(l); return <CiteMark key={l} n={n} onClick={() => onCite(n)} />; });
  const sourceItems = cites.labels.filter((l) => byLabel[l]).map((l) => ({ n: cites.num(l), ...sourceTitle(byLabel[l]) }));

  return (
    <div className="space-y-6">
      <Card>
        <div className="px-6 pt-5">
          <p className="flex items-center gap-2 text-[13px] font-semibold uppercase tracking-wide text-[var(--ink-3)]"><Sparkles size={15} className="text-[var(--brand)]" /> In short</p>
          <p className="mt-2.5 text-[16px] leading-[1.7] text-[var(--ink)]">{summary}</p>
          {llm.tip && (
            <div className="mt-4 flex gap-3 rounded-lg bg-[var(--surface-2)] px-4 py-3 ring-1 ring-[var(--line)]">
              <Lightbulb size={17} className="mt-0.5 shrink-0 text-[var(--warn)]" />
              <p className="text-[14px] leading-relaxed text-[var(--ink-2)]"><Inline text={llm.tip} cites={cites} />{tipCites}</p>
            </div>
          )}
          <p className="mt-4 flex items-center gap-1.5 pb-5 text-xs text-[var(--ink-3)]">
            <ShieldCheck size={13} className="text-[var(--good)]" />
            {r.status === "REPAIRED" ? "Checked against your records (corrected once)" : "Checked against your records"} · tap a number to see its source
          </p>
        </div>
        <div className="border-t border-[var(--line)]">
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
                  className="flex w-full items-center justify-between px-6 py-3.5 text-left text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--surface-2)]">
            <span>Sources ({sourceItems.length}) and evidence</span>
            <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
          {open && (
            <div className="space-y-5 px-6 pb-6">
              <SourceList items={sourceItems} highlight={hl} />
              {cycle && <EvidencePanel cycle={cycle} />}
              {r.trace && (
                <details className="rounded-lg border border-[var(--line)] px-4 py-3">
                  <summary className="flex cursor-pointer items-center gap-1.5 text-sm font-semibold"><BrainCircuit size={15} /> How this explanation was produced</summary>
                  <div className="mt-3"><AgentTraceView trace={r.trace} /></div>
                </details>
              )}
            </div>
          )}
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="What changed" subtitle="Biggest changes first" />
          <div className="px-6 pb-6 pt-4">
            {drivers.length ? <ul className="divide-y divide-[var(--line)]">{drivers}</ul>
              : <p className="text-[14px] text-[var(--ink-2)]">Nothing changed compared with the previous month.</p>}
          </div>
        </Card>
        <Card>
          <CardHeader title="Top usage drivers" subtitle="Charges beyond your monthly plan fee" />
          <div className="px-6 pb-6 pt-4">
            {usage.length ? <ul className="divide-y divide-[var(--line)]">{usage}</ul>
              : <div className="flex items-center gap-3 rounded-lg bg-[var(--good-bg)] px-4 py-3 text-sm font-medium text-[var(--good)]"><ShieldCheck size={18} /> Everything was covered by your plan.</div>}
          </div>
        </Card>
      </div>
    </div>
  );
}
