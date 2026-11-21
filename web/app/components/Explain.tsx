import { Suspense, useState } from "react";
import { CitedText, SourcesPanel } from "./Sources";
import { Await, Link } from "react-router";
import { ShieldCheck, Sparkles, TrendingDown, TrendingUp } from "lucide-react";
import type { Explanation, ExplanationResponse } from "~/lib/types";
import { cycleLabel, money } from "~/lib/format";
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

export function WhatChanged({ ex, locale, title }: { ex: Explanation; locale: string; title?: string }) {
  const max = Math.max(1, ...ex.drivers.map((d) => Math.abs(d.delta)));
  return (
    <Card>
      <CardHeader title={title ?? "What changed"}
        subtitle={ex.previous_cycle ? `${cycleLabel(ex.cycle, locale)} vs ${cycleLabel(ex.previous_cycle, locale)}, before VAT` : "Your first bill"}
        action={<DeltaPill ex={ex} locale={locale} />} />
      <div className="px-6 pb-6 pt-4">
        {ex.drivers.length === 0 ? (
          <p className="text-sm text-[var(--ink-3)]">{ex.previous_cycle ? "Same charges as the previous cycle." : ex.facts[0]}</p>
        ) : (
          <ul className="space-y-4">
            {ex.drivers.slice(0, 5).map((d) => (
              <li key={d.code}>
                <div className="flex items-baseline justify-between gap-4">
                  <span className="text-[15px] font-semibold">{d.label}</span>
                  <span className={`tabular text-[15px] font-bold ${d.delta > 0 ? "text-[var(--bad)]" : "text-[var(--good)]"}`}>
                    {d.delta > 0 ? "+" : "−"}{money(Math.abs(d.delta), ex.currency, locale)}
                  </span>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--chip)]">
                  <div className="h-full rounded-full" style={{ width: `${(Math.abs(d.delta) / max) * 100}%`, background: d.delta > 0 ? "var(--bad)" : "var(--good)" }} />
                </div>
                <p className="mt-1.5 text-[13px] text-[var(--ink-3)]">{d.detail.replace(/^[^:]+:\s*/, "").replace(/\s*\([^)]*before VAT\)/, "")}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

export function TopDrivers({ ex, locale }: { ex: Explanation; locale: string }) {
  return (
    <Card>
      <CardHeader title="Top usage drivers" subtitle="Charges beyond your monthly plan fee" />
      <div className="px-6 pb-6 pt-4">
        {ex.top_usage_drivers.length === 0 ? (
          <div className="flex items-center gap-3 rounded-lg bg-[var(--good-bg)] p-4 text-sm font-medium text-[var(--good)]">
            <ShieldCheck size={18} /> Everything was covered by your plan. No extra charges.
          </div>
        ) : (
          <ul className="space-y-4">
            {ex.top_usage_drivers.map((d) => (
              <li key={d.code}>
                <div className="flex items-baseline justify-between gap-4">
                  <span className="text-[15px] font-semibold">{d.label}</span>
                  <span className="tabular text-[15px]"><b>{money(d.amount, ex.currency, locale)}</b> <span className="text-[var(--ink-3)]">· {d.share_pct.toFixed(0)}%</span></span>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--brand-bg)]"><div className="h-full rounded-full bg-[var(--brand)]" style={{ width: `${d.share_pct}%` }} /></div>
                <p className="mt-1.5 text-[13px] text-[var(--ink-3)]">{d.detail.split(": ")[1]?.split(" = ")[0]}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

const LANGS = [["en", "English"], ["de", "Deutsch"], ["pl", "Polski"]] as const;

export function Narrative({ narrative, lang, basePath, title = "In plain words", languages = true, cycle }:
  { narrative: Promise<ExplanationResponse> | ExplanationResponse; lang: string; basePath: string; title?: string; languages?: boolean; cycle?: string }) {
  const [open, setOpen] = useState(false);
  const [hl, setHl] = useState<string | null>(null);
  const cite = (label: string) => {
    setOpen(true); setHl(label);
    setTimeout(() => document.getElementById(`src-${label}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  };
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] bg-[var(--surface-2)] px-6 py-3.5">
        <span className="inline-flex items-center gap-2 text-[15px] font-semibold"><Sparkles size={17} className="text-[var(--brand)]" /> {title}</span>
        {languages && <div className="flex rounded-lg bg-[var(--chip)] p-0.5" role="tablist" aria-label="Language">
          {LANGS.map(([k, l]) => (
            <Link key={k} to={`${basePath}?lang=${k}`} preventScrollReset replace role="tab" aria-selected={lang === k}
                  className={`rounded-md px-2.5 py-1 text-xs font-semibold ${lang === k ? "bg-[var(--surface)] text-[var(--ink)] shadow-sm" : "text-[var(--ink-3)] hover:text-[var(--ink)]"}`}>{l}</Link>
          ))}
        </div>}
      </div>
      <div className="px-6 py-5">
        <Suspense key={lang} fallback={<div className="space-y-2.5" aria-busy="true"><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-4/5" /><Skeleton className="h-4 w-2/3" /></div>}>
          <Await resolve={narrative} errorElement={<p className="text-sm text-[var(--ink-3)]">The written explanation is unavailable right now. The breakdown is complete.</p>}>
            {(r) => r.narrative ? (
              <div className="fade-up">
                <CitedText text={r.narrative} onCite={cite} />
                <p className="mt-4 flex flex-wrap items-center gap-1.5 text-xs text-[var(--ink-3)]">
                  <ShieldCheck size={13} />
                  {r.status === "VERIFIED" && <span className="font-semibold text-[var(--good)]">Verified</span>}
                  {r.status === "REPAIRED" && <span className="font-semibold text-[var(--good)]">Verified after self-correction</span>}
                  {r.status === "UNVERIFIED" && <span className="font-semibold text-[var(--warn)]">Not verified</span>}
                  <span>· Answered by the billing agent ({r.provider.split(":")[1] ?? r.provider}) from documents built from your records. Every number is checked against the cited documents.</span>
                </p>
              </div>
            ) : (
              <p className="text-sm text-[var(--ink-3)]">The AI explanation is unavailable right now{r.trace?.degraded?.length ? ` (${r.trace.degraded[0]})` : ""}. The breakdown below is exact and computed from your CDRs.</p>
            )}
          </Await>
        </Suspense>
      </div>
      {cycle && (
        <Suspense fallback={null}>
          <Await resolve={narrative} errorElement={null}>
            {(r) => <SourcesPanel citations={r.citations ?? []} trace={r.trace} cycle={cycle} open={open} setOpen={setOpen} highlight={hl} />}
          </Await>
        </Suspense>
      )}
    </Card>
  );
}
