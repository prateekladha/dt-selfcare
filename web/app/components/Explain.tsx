import { Suspense } from "react";
import { Await, Link } from "react-router";
import type { Explanation, ExplanationResponse } from "~/lib/types";
import { cycleLabel, money } from "~/lib/format";
import { Card, Pill } from "./ui";

export function WhyChanged({ ex, locale, title }: { ex: Explanation; locale: string; title?: string }) {
  const cur = ex.currency;
  const heading = title ?? (ex.direction === "UP" ? "Why your bill increased" : ex.direction === "DOWN" ? "Why your bill went down" : "Your bill");
  const maxDelta = Math.max(1, ...ex.drivers.map((d) => Math.abs(d.delta)));
  return (
    <Card title={heading} action={ex.delta_total !== null && (
      <Pill tone={ex.direction === "UP" ? "bad" : ex.direction === "DOWN" ? "good" : "neutral"}>
        {ex.direction === "UP" ? "▲" : ex.direction === "DOWN" ? "▼" : "="} {money(Math.abs(ex.delta_total), cur, locale)}
        {ex.delta_pct !== null && ` (${Math.abs(ex.delta_pct).toFixed(1)}%)`}
      </Pill>)}>
      {ex.previous_cycle && ex.previous_total !== null ? (
        <p className="mb-4 text-sm text-[var(--ink-2)]">
          {cycleLabel(ex.cycle, locale)}: <b className="text-[var(--ink)]">{money(ex.current_total, cur, locale)}</b> vs{" "}
          {cycleLabel(ex.previous_cycle, locale)}: {money(ex.previous_total, cur, locale)} (incl. VAT)
        </p>
      ) : <p className="mb-4 text-sm text-[var(--ink-2)]">{ex.facts[0]}</p>}
      {ex.drivers.length > 0 ? (
        <ul className="space-y-3" aria-label="What changed, biggest first">
          {ex.drivers.slice(0, 5).map((d) => (
            <li key={d.code}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="font-medium text-[var(--ink)]">{d.label}</span>
                <span className={`tabular-nums font-semibold ${d.delta > 0 ? "text-[var(--bad)]" : "text-[var(--good)]"}`}>
                  {d.delta > 0 ? "+" : "−"}{money(Math.abs(d.delta), cur, locale)}
                </span>
              </div>
              <div className="mt-1 h-1.5 rounded-full bg-[var(--chip)]">
                <div className="h-full rounded-full" style={{ width: `${(Math.abs(d.delta) / maxDelta) * 100}%`, background: d.delta > 0 ? "var(--bad)" : "var(--good)" }} />
              </div>
              <p className="mt-1 text-xs text-[var(--ink-3)]">{d.detail}</p>
            </li>
          ))}
        </ul>
      ) : ex.previous_cycle && <p className="text-sm text-[var(--ink-3)]">No change in charges compared to last cycle.</p>}
      {ex.usage_changes.length > 0 && (
        <div className="mt-4 border-t border-[var(--line)] pt-3">
          <p className="mb-1 text-xs font-semibold uppercase text-[var(--ink-3)]">Usage</p>
          <ul className="space-y-0.5 text-xs text-[var(--ink-2)]">{ex.usage_changes.map((u) => <li key={u}>{u}</li>)}</ul>
        </div>
      )}
    </Card>
  );
}

export function TopDrivers({ ex, locale }: { ex: Explanation; locale: string }) {
  return (
    <Card title="Top usage drivers">
      {ex.top_usage_drivers.length === 0 ? (
        <p className="text-sm text-[var(--ink-2)]">Everything was covered by your plan fee: no extra charges this cycle. 🎉</p>
      ) : (
        <ul className="space-y-3" aria-label="Charges beyond the plan fee">
          {ex.top_usage_drivers.map((d) => (
            <li key={d.code}>
              <div className="flex justify-between text-sm">
                <span className="text-[var(--ink)]">{d.label}</span>
                <span className="tabular-nums text-[var(--ink-2)]">{money(d.amount, ex.currency, locale)} · {d.share_pct}%</span>
              </div>
              <div className="mt-1 h-1.5 rounded-full bg-[var(--brand-bg)]">
                <div className="h-full rounded-full bg-[var(--brand)]" style={{ width: `${d.share_pct}%` }} />
              </div>
              <p className="mt-1 text-xs text-[var(--ink-3)]">{d.detail}</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const LANGS = [["en", "EN"], ["de", "DE"], ["pl", "PL"]] as const;

export function Narrative({ narrative, lang, basePath }: { narrative: Promise<ExplanationResponse> | ExplanationResponse; lang: string; basePath: string }) {
  return (
    <Card title={<span className="inline-flex items-center gap-2">✨ Explained in plain words</span>}
          action={<div className="flex gap-1">{LANGS.map(([k, l]) => (
            <Link key={k} to={`${basePath}?lang=${k}`} preventScrollReset
                  className={`rounded-md px-2 py-0.5 text-xs font-semibold ${lang === k ? "bg-[var(--brand)] text-white" : "text-[var(--ink-2)] hover:bg-[var(--chip)]"}`}>{l}</Link>))}</div>}>
      <Suspense fallback={<div className="space-y-2" aria-busy="true"><div className="h-3 w-full animate-pulse rounded bg-[var(--chip)]" /><div className="h-3 w-4/5 animate-pulse rounded bg-[var(--chip)]" /><div className="h-3 w-3/5 animate-pulse rounded bg-[var(--chip)]" /></div>}>
        <Await resolve={narrative} errorElement={<p className="text-sm text-[var(--ink-3)]">The explanation is unavailable right now; the breakdown above is complete.</p>}>
          {(r) => (
            <div>
              <p className="whitespace-pre-line text-[15px] leading-relaxed text-[var(--ink)]">{r.narrative}</p>
              <p className="mt-3 text-[11px] text-[var(--ink-3)]">
                {r.provider === "template" ? "Generated from your bill data." : `AI-written (${r.provider.replace("mistral:", "Mistral ")}) from verified bill facts. Every number is checked against your bill.`}
              </p>
            </div>
          )}
        </Await>
      </Suspense>
    </Card>
  );
}
