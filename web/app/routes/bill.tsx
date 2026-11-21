import { ArrowLeft, Download } from "lucide-react";
import { Link } from "react-router";
import { DeltaPill, Narrative, TopDrivers, WhatChanged } from "~/components/Explain";
import { PayButton } from "~/components/PayButton";
import { Card, CardHeader, LinkButton, Pill } from "~/components/ui";
import { api, PRIVATE } from "~/lib/api.server";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import type { ExplanationResponse, Line } from "~/lib/types";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/bill";

export const headers = () => PRIVATE;
export const meta = ({ params }: Route.MetaArgs) => [{ title: `${params.cycle === "current" ? "This month" : `Bill ${params.cycle}`} · Self-care` }];

type BillDetail = { id: string; cycle: string; total: number; subtotal: number; vat: number; vat_rate: string; currency: string;
  status: string; outstanding: number; due_date: string; issued_at: string; lines: Line[]; plan_code: string };

const GROUPS: [string, string][] = [["PLAN", "Plan"], ["ADDON", "Add-ons"], ["OVERAGE", "Beyond your allowance"], ["ROAMING", "Roaming"], ["INTERNATIONAL", "International"]];

export async function loader({ request, params }: Route.LoaderArgs) {
  const lang = new URL(request.url).searchParams.get("lang") ?? "en";
  const cycle = params.cycle;
  const [facts, bill] = await Promise.all([
    api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?ai=false`),
    cycle === "current" ? Promise.resolve(null) : api<BillDetail>(request, `/v1/me/bills/${cycle}`),
  ]);
  const narrative = api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?lang=${lang}`);
  return { facts, narrative, bill, lang, cycle };
}

export default function BillPage({ loaderData }: Route.ComponentProps) {
  const { facts, narrative, bill, lang, cycle } = loaderData;
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
              {bill && (bill.outstanding > 0 ? <Pill tone={new Date(bill.due_date) < new Date(home.as_of) ? "bad" : "warn"}>Due {dateLabel(bill.due_date, loc)}</Pill> : <Pill tone="good">Paid</Pill>)}
              <DeltaPill ex={ex} locale={loc} />
            </div>
          </div>
          <div className="text-right">
            <p className="display tabular text-[44px] font-extrabold leading-none">{money(total, cur, loc)}</p>
            <p className="mt-1.5 text-[13px] text-[var(--ink-3)]">incl. {vatPct}% VAT</p>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              {bill && bill.outstanding > 0 && home.features.payments && <PayButton purpose="BILL" billId={bill.id} label={`Pay ${money(bill.outstanding, cur, loc)}`} name={home.subscriber.name} contact={`+${home.subscriber.id}`} email={home.subscriber.email} />}
              {isCurrent && home.features.payments && total > 0 && <PayButton purpose="ADVANCE" label="Pay in advance" name={home.subscriber.name} contact={`+${home.subscriber.id}`} email={home.subscriber.email} />}
              {bill && <LinkButton to={`/bills/${cycle}/pdf`} variant="secondary" download><Download size={16} /> PDF</LinkButton>}
            </div>
          </div>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-5">
        <div className="space-y-6 lg:col-span-3">
          <Narrative narrative={narrative} lang={lang} basePath={`/bills/${cycle}`} title={isCurrent ? "This month in plain words" : "This bill in plain words"} cycle={cycle} />
          <WhatChanged ex={ex} locale={loc} title={isCurrent ? "So far vs your last bill" : "What changed"} />
        </div>
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <CardHeader title="Breakdown" subtitle="Before VAT unless stated" />
            <div className="px-6 pb-6 pt-3">
              {GROUPS.map(([cat, label]) => {
                const items = lines.filter((l) => l.category === cat);
                if (!items.length) return null;
                return (
                  <div key={cat} className="border-b border-[var(--line)] py-3 last:border-0">
                    <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">{label}</p>
                    {items.map((l) => (
                      <div key={l.code} className="flex items-baseline justify-between gap-3 py-1 text-sm">
                        <span>{l.label}<span className="ml-1.5 text-[var(--ink-3)]">{l.quantity.toLocaleString(loc)} {l.unit}</span></span>
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
          <TopDrivers ex={ex} locale={loc} />
        </div>
      </div>
    </div>
  );
}
