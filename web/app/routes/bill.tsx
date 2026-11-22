import { ArrowLeft, BarChart3, FileText } from "lucide-react";
import { Suspense } from "react";
import { Await } from "react-router";
import { DailyUsageChart, type DailyUsage } from "~/components/DailyUsageChart";
import { Link } from "react-router";
import { DeltaPill, ExplanationSection } from "~/components/Explain";
import { PayLink } from "~/components/PayLink";
import { Card, CardHeader, LinkButton, Pill } from "~/components/ui";
import { ApiError, api, streamed, PRIVATE } from "~/lib/api.server";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import type { ExplanationResponse, Line } from "~/lib/types";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/bill";

export const headers = () => PRIVATE;
export const meta = ({ params }: Route.MetaArgs) => [{ title: `${params.cycle === "current" ? "This month" : `Bill ${params.cycle}`} · Self-care` }];

type BillDetail = { id: string; cycle: string; total: number; subtotal: number; vat: number; vat_rate: string; currency: string;
  status: string; outstanding: number; due_date: string; issued_at: string; lines: Line[]; plan_code: string };

// Every line category the rating engine produces; anything new lands in "Other" instead of vanishing (lines
// missing here made the breakdown add up to less than the total printed under it).
const GROUPS: [string, string][] = [["PLAN", "Plan"], ["ADDON", "Add-ons"], ["OVERAGE", "Beyond your allowance"], ["ROAMING", "Roaming"],
  ["INTERNATIONAL", "International"], ["MESSAGING", "Messaging"], ["THIRD_PARTY", "Third-party services"], ["ADJUSTMENT", "Adjustments"]];
const KNOWN = new Set(GROUPS.map(([c]) => c));

export async function loader({ request, params }: Route.LoaderArgs) {
  const asked = new URL(request.url).searchParams.get("lang") ?? "en";
  const lang = ["en", "de", "pl"].includes(asked) ? asked : "en";
  const cycle = params.cycle;
  if (!/^(\d{4}-(0[1-9]|1[0-2])|current)$/.test(cycle)) throw new Response("Not found", { status: 404 });  // never forwarded raw into an API path
  let facts: ExplanationResponse, bill: BillDetail | null;
  try {
    [facts, bill] = await Promise.all([
      api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?ai=false`),
      cycle === "current" ? Promise.resolve(null) : api<BillDetail>(request, `/v1/me/bills/${cycle}`),
    ]);
  } catch (e) {   // a month with no bill (not issued yet / before your history) is "not found", not an outage
    if (e instanceof ApiError && e.status === 404) throw new Response("Not found", { status: 404 });
    throw e;
  }
  const narrative = streamed(api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?lang=${lang}`));  // LLM, streamed
  const daily = streamed(api<DailyUsage>(request, `/v1/me/usage/daily${cycle === "current" ? "" : `?cycle=${cycle}`}`));  // streamed
  return { facts, narrative, daily, bill, lang, cycle };
}

export default function BillPage({ loaderData }: Route.ComponentProps) {
  const { facts, narrative, daily, bill, lang, cycle } = loaderData;
  const { home } = useSnapshot();
  const loc = home.subscriber.locale;
  const isCurrent = cycle === "current";
  const lines = bill?.lines ?? home.unbilled.lines;
  const cur = bill?.currency ?? home.unbilled.currency;
  const total = bill?.total ?? home.unbilled.total;
  const ex = facts.explanation;
  const vatPct = bill ? Math.round(Number(bill.vat_rate) * 100) : Math.round((home.unbilled.vat / Math.max(home.unbilled.subtotal, 1)) * 100);

  return (
    <div className="space-y-6">
      <Link to="/bills" className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--ink-3)] hover:text-[var(--ink)]"><ArrowLeft size={16} /> All bills</Link>
      <Card>
        <div className="flex flex-wrap items-end justify-between gap-6 p-6">
          <div>
            <p className="text-sm font-semibold text-[var(--ink-3)]">{isCurrent ? "This month so far · not billed yet" : `Bill · ${home.plan.name}`}</p>
            <h1 className="display mt-1 text-[34px] font-extrabold leading-tight">{cycleLabel(isCurrent ? home.unbilled.cycle : cycle, loc)}</h1>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {bill && (bill.outstanding > 0 ? <Pill tone={new Date(bill.due_date) < new Date(home.as_of) ? "bad" : "warn"}>{new Date(bill.due_date) < new Date(home.as_of) ? "Overdue · due" : "Due"} {dateLabel(bill.due_date, loc)}</Pill> : <Pill tone="good">Paid</Pill>)}
              <DeltaPill ex={ex} locale={loc} />
            </div>
          </div>
          {/* phones: amount + actions full width, left-aligned under the title; tablet/desktop: right column */}
          <div className="w-full sm:w-auto sm:text-right">
            <p className="display tabular text-[44px] font-extrabold leading-none">{money(total, cur, loc)}</p>
            <p className="mt-1.5 text-[13px] text-[var(--ink-3)]">incl. {vatPct}% VAT</p>
            <div className="mt-4 flex flex-wrap gap-2 sm:justify-end">
              {bill && bill.outstanding > 0 && home.features.payments && <PayLink purpose="BILL" cycle={bill.cycle} label={`Pay ${money(bill.outstanding, cur, loc)}`} />}
              {isCurrent && home.features.payments && total > 0 && <PayLink purpose="ADVANCE" label="Pay in advance" />}
              {bill && <LinkButton to={`/bills/${cycle}/pdf`} variant="secondary" target="_blank" rel="noopener"><FileText size={16} /> PDF</LinkButton>}
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader icon={<BarChart3 size={18} />} title={`Daily usage · ${cycleLabel(isCurrent ? home.unbilled.cycle : cycle, loc)}`} subtitle="Every day of the month, from your network usage records" />
        <div className="pb-4 pt-3">
          <Suspense fallback={<div className="mx-6 h-[220px] animate-pulse rounded-lg bg-[var(--chip)]" />}>
            <Await resolve={daily} errorElement={<p className="px-6 pb-4 text-sm text-[var(--ink-3)]">Daily usage isn't available right now.</p>}>
              {(du) => <DailyUsageChart usage={du} monthLabel={cycleLabel(isCurrent ? home.unbilled.cycle : cycle, loc)} />}
            </Await>
          </Suspense>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-2">
          <Card>
            <CardHeader title="Breakdown" subtitle="Before VAT unless stated" />
            <div className="px-6 pb-6 pt-3">
              {[...GROUPS, ["OTHER", "Other"] as [string, string]].map(([cat, label]) => {
                const items = lines.filter((l) => (cat === "OTHER" ? !KNOWN.has(l.category) : l.category === cat));
                if (!items.length) return null;
                return (
                  <div key={cat} className="border-b border-[var(--line)] py-3 last:border-0">
                    <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">{label}</p>
                    {items.map((l) => (
                      <div key={l.code} className="flex items-baseline justify-between gap-3 py-1 text-sm">
                        <span>{l.label}<span className="ml-1.5 text-[var(--ink-3)]">{l.quantity.toLocaleString("en-GB")} {l.unit}</span></span>
                        <span className="tabular font-semibold">{money(l.amount, cur, loc)}</span>
                      </div>
                    ))}
                  </div>
                );
              })}
              <div className="mt-2 space-y-1 border-t-2 border-[var(--ink)] pt-3 text-sm">
                <div className="flex justify-between"><span className="text-[var(--ink-3)]">Subtotal</span><span className="tabular">{money(bill?.subtotal ?? home.unbilled.subtotal, cur, loc)}</span></div>
                <div className="flex justify-between"><span className="text-[var(--ink-3)]">VAT {vatPct}%</span><span className="tabular">{money(bill?.vat ?? home.unbilled.vat, cur, loc)}</span></div>
                <div className="flex justify-between pt-1 text-base font-extrabold"><span>Total</span><span className="tabular">{money(total, cur, loc)}</span></div>
              </div>
            </div>
          </Card>
        </div>
        <div className="lg:col-span-3">
          <ExplanationSection data={narrative} lang={lang} basePath={`/bills/${cycle}`} cycle={cycle}
                              heading={isCurrent ? "This month so far, explained" : "Why this bill looks like this"} />
        </div>
      </div>
    </div>
  );
}
