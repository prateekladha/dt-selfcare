import { ArrowRight, BarChart3, CalendarClock, CheckCircle2, FileText, Gauge, MessageSquare, Phone, Plane, Receipt, Sparkles, Wallet } from "lucide-react";
import { Link } from "react-router";
import { ExplanationSection } from "~/components/Explain";
import { Suspense } from "react";
import { Await } from "react-router";
import { PayLink } from "~/components/PayLink";
import { AddDataButton } from "~/components/OrderCard";
import { TrendChart } from "~/components/TrendChart";
import { DailyUsageChart, type DailyUsage } from "~/components/DailyUsageChart";
import { Card, CardHeader, LinkButton, Pill } from "~/components/ui";
import { api, streamed, PRIVATE } from "~/lib/api.server";
import { cycleLabel, dateLabel, mbToGb, money } from "~/lib/format";
import type { ExplanationResponse } from "~/lib/types";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/home";

export const meta = () => [{ title: "Overview · Self-care" }];
export const headers = () => PRIVATE;

// The deterministic explanation is part of the server render; only the AI narrative streams in later.
export async function loader({ request }: Route.LoaderArgs) {
  const lang = new URL(request.url).searchParams.get("lang") ?? "en";
  // one call, not list-then-fetch; the LLM explanation streams in after the page renders (cached per bill)
  const explanation = streamed(api<ExplanationResponse>(request, `/v1/me/bills/latest/explanation?lang=${lang}`));
  const daily = streamed(api<DailyUsage>(request, "/v1/me/usage/daily"));   // streamed: never blocks the page
  return { explanation, daily, lang };
}

export default function Overview({ loaderData }: Route.ComponentProps) {
  const { explanation, daily, lang } = loaderData;
  const last = useSnapshot().home.last_bill?.cycle ?? null;
  const { home, d } = useSnapshot();
  const loc = home.subscriber.locale;
  const u = home.usage;
  const cur = home.unbilled.currency;

  const pct = u.data_pct;
  const projPct = (d.projectedMb / Math.max(u.data_allowance_mb, 1)) * 100;
  const meterColor = pct >= 100 ? "var(--bad)" : pct >= 80 ? "var(--warn)" : "var(--brand)";
  const roamingMb = Object.values(u.roaming_data_mb).reduce((a, b) => a + b, 0) + u.eu_roaming_data_mb;
  const avg = home.trend.length ? home.trend.reduce((s, b) => s + b.total, 0) / home.trend.length : 0;
  const peak = home.trend.reduce((m, b) => (b.total > (m?.total ?? -1) ? b : m), home.trend[0]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-[var(--ink-3)]">{d.greeting},</p>
          <h1 className="display text-[34px] font-extrabold leading-tight">{home.subscriber.name.split(" ")[0]}</h1>
        </div>
        <p className="text-[13px] text-[var(--ink-3)]">{home.plan.name} · <Link to="/plan" className="font-semibold text-[var(--ink)] hover:underline">Change tariff</Link> · updated {d.asOfLabel}</p>
      </div>

      {explanation && (
        <Suspense fallback={null}>
          {/* errorElement must be a real element: React Router treats null/undefined as "none" and rethrows
              the rejection to the route's error boundary, which would replace the whole page. */}
          <Await resolve={explanation} errorElement={<></>}>
            {(r) => r.llm?.summary ? (
              <Link to={`/bills/${last}`} className="group flex items-center gap-4 rounded-[var(--radius)] border border-[var(--brand)]/25 bg-[var(--brand-bg)] px-5 py-4 transition-colors hover:border-[var(--brand)]">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[var(--brand)] text-white"><Sparkles size={18} /></span>
                <p className="text-[15px] leading-snug text-[var(--ink)]">{r.llm.summary.replace(/\s*\[[DGS]\d{1,2}\]/g, "").split(/(?<=\.)\s/)[0]}</p>
                <span className="ml-auto hidden shrink-0 items-center gap-1 text-sm font-bold text-[var(--brand-ink)] sm:flex">See why <ArrowRight size={16} className="transition-transform group-hover:translate-x-0.5" /></span>
              </Link>
            ) : null}
          </Await>
        </Suspense>
      )}

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        {/* Amount due: the primary job of the page */}
        <Card className="flex flex-col">
          <CardHeader icon={<Wallet size={18} />} title="Amount due"
            action={d.due ? <Pill tone={d.overdue ? "bad" : "warn"}>{d.overdue ? "Overdue" : `Due ${dateLabel(d.due.due_date, loc)}`}</Pill> : <Pill tone="good" icon={<CheckCircle2 size={13} />}>Paid</Pill>} />
          <div className="flex flex-1 flex-col px-6 pb-6 pt-4">
            <p className="display tabular text-[40px] font-extrabold leading-none">{money(home.outstanding.amount, home.outstanding.currency, loc)}</p>
            <p className="mt-2 text-[13px] text-[var(--ink-3)]">{d.due ? `${cycleLabel(d.due.cycle, loc)} bill, including VAT · ${d.overdue ? "was due" : "due"} ${dateLabel(d.due.due_date, loc)}` : "You're all paid up. Nice."}</p>
            <div className="mt-auto pt-6">
              <div className="flex flex-wrap gap-2">
                {d.due && home.features.payments && <PayLink purpose="BILL" cycle={d.due.cycle} label="Pay now" />}
                {home.last_bill && <LinkButton to={`/bills/${home.last_bill.cycle}`} variant="secondary"><Receipt size={16} /> View bill</LinkButton>}
              </div>
              {home.last_bill && <a href={`/bills/${home.last_bill.cycle}/pdf`} target="_blank" rel="noopener" className="mt-3 inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--ink-2)] underline-offset-4 hover:underline"><FileText size={15} /> View PDF</a>}
            </div>
          </div>
        </Card>

        {/* Unbilled so far */}
        <Card className="flex flex-col">
          <CardHeader icon={<CalendarClock size={18} />} title="This month so far" subtitle={`${cycleLabel(u.cycle, loc)} · not billed yet`} />
          <div className="flex flex-1 flex-col px-6 pb-6 pt-4">
            <p className="display tabular text-[40px] font-extrabold leading-none">{money(home.unbilled.total, cur, loc)}</p>
            <div className="mt-4">
              <div className="flex justify-between text-xs text-[var(--ink-3)]"><span>Day {d.day} of {d.daysInCycle}</span><span>{d.daysLeft} days left</span></div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[var(--chip)]"><div className="h-full rounded-full bg-[var(--ink)]" style={{ width: `${(d.day / d.daysInCycle) * 100}%` }} /></div>
            </div>
            <ul className="mt-4 space-y-1.5 text-sm">
              {home.unbilled.lines.slice(0, 3).map((l) => (
                <li key={l.code} className="flex justify-between gap-3"><span className="truncate text-[var(--ink-2)]">{l.label}</span><span className="tabular font-medium">{money(l.amount, cur, loc)}</span></li>
              ))}
            </ul>
            <div className="mt-auto flex flex-wrap items-center gap-2 pt-5">
              {home.features.payments && home.unbilled.total > 0 && <PayLink purpose="ADVANCE" label="Pay in advance" variant="secondary" />}
              <LinkButton to="/bills/current" variant="ghost">Explain <ArrowRight size={16} /></LinkButton>
            </div>
          </div>
        </Card>

        {/* Data with month-end projection */}
        <Card className="flex flex-col">
          <CardHeader icon={<Gauge size={18} />} title="Mobile data" subtitle={`${d.daysLeft} days until your allowance resets`} />
          <div className="flex flex-1 flex-col px-6 pb-6 pt-4">
            <p className="display tabular text-[40px] font-extrabold leading-none">{mbToGb(u.data_mb)}<span className="ml-1.5 text-lg font-bold text-[var(--ink-3)]">/ {mbToGb(u.data_allowance_mb)} GB</span></p>
            <div className="relative mt-5 h-2.5 overflow-visible rounded-full bg-[var(--chip)]" role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label="Data used">
              <div className="h-full rounded-full" style={{ width: `${Math.min(pct, 100)}%`, background: meterColor }} />
              {projPct > pct && projPct <= 130 && <div className="absolute top-[-3px] h-4 w-0.5 rounded bg-[var(--ink-3)]" style={{ left: `${Math.min(projPct, 100)}%` }} title="Projected at month end" />}
            </div>
            <p className={`mt-3 text-[13px] font-medium ${projPct > 100 ? "text-[var(--warn)]" : "text-[var(--ink-3)]"}`}>
              {pct >= 100 ? (home.plan.overage_policy === "THROTTLE" ? "Allowance used: speed is reduced until reset." : "Allowance used: extra data is charged.")
                : projPct > 100 ? `At this pace you'll need ~${mbToGb(d.projectedMb)} GB by month end. Consider a data add-on.`
                : `${Math.round(pct)}% used · on track for ~${mbToGb(d.projectedMb)} GB this month`}
            </p>
            <div className="mt-4"><AddDataButton country={home.subscriber.country} /></div>
            <dl className="mt-auto grid grid-cols-3 gap-2 pt-5 text-center">
              {[[Phone, `${u.voice_min}`, "min"], [MessageSquare, `${u.sms}`, "SMS"], [Plane, `${roamingMb}`, "MB roaming"]].map(([Icon, v, l]) => {
                const I = Icon as typeof Phone;
                return (<div key={l as string} className="rounded-lg bg-[var(--surface-2)] px-2 py-2.5 ring-1 ring-[var(--line)]"><I size={15} className="mx-auto text-[var(--ink-3)]" /><dd className="tabular mt-1 text-sm font-bold">{v as string}</dd><dt className="text-[11px] text-[var(--ink-3)]">{l as string}</dt></div>);
              })}
            </dl>
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader icon={<BarChart3 size={18} />} title={`Daily usage · ${cycleLabel(u.cycle, loc)}`} subtitle="Every day of the month, from your network usage records" />
        <div className="pb-4 pt-3">
          <Suspense fallback={<div className="mx-6 h-[220px] animate-pulse rounded-lg bg-[var(--chip)]" />}>
            <Await resolve={daily} errorElement={<p className="px-6 pb-4 text-sm text-[var(--ink-3)]">Daily usage isn't available right now.</p>}>
              {(du) => <DailyUsageChart usage={du} monthLabel={cycleLabel(u.cycle, loc)} />}
            </Await>
          </Suspense>
        </div>
      </Card>

      <Card>
        <CardHeader title="Your bills over 12 months" subtitle="Tap a month to see its bill"
          action={peak && <div className="hidden gap-6 text-right sm:flex">
            <div><p className="text-xs text-[var(--ink-3)]">Average</p><p className="tabular text-sm font-bold">{money(Math.round(avg), cur, loc)}</p></div>
            <div><p className="text-xs text-[var(--ink-3)]">Highest</p><p className="tabular text-sm font-bold">{money(peak.total, cur, loc)} <span className="font-normal text-[var(--ink-3)]">{cycleLabel(peak.cycle, loc, true)}</span></p></div>
          </div>} />
        <div className="pt-4"><TrendChart bills={home.trend} locale={loc} selected={home.last_bill?.cycle} /></div>
        <div className="flex flex-wrap gap-5 border-t border-[var(--line)] px-6 py-3 text-xs text-[var(--ink-3)]">
          <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[var(--brand)]" />Paid</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm border-2 border-[var(--brand)]" />Unpaid</span>
          <Link to="/bills" className="ml-auto font-semibold text-[var(--ink)] hover:underline">All bills →</Link>
        </div>
      </Card>

      {explanation && last && (
        <ExplanationSection data={explanation} lang={lang} basePath="/" cycle={last}
                            heading={`Why your ${cycleLabel(last, loc)} bill looks like this`} />
      )}
    </div>
  );
}
