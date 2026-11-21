import { Link } from "react-router";
import { Narrative, TopDrivers, WhyChanged } from "~/components/Explain";
import { PayButton } from "~/components/PayButton";
import { Card, Pill } from "~/components/ui";
import { api, PRIVATE } from "~/lib/api.server";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import type { ExplanationResponse, Home, Line } from "~/lib/types";
import type { Route } from "./+types/bill";

export const headers = () => PRIVATE;
export const meta = ({ params }: Route.MetaArgs) => [{ title: `Bill ${params.cycle} · Mobile self-care` }];

type BillDetail = { id: string; cycle: string; total: number; subtotal: number; vat: number; vat_rate: string; currency: string;
  status: string; outstanding: number; due_date: string; lines: Line[]; plan_code: string };

export async function loader({ request, params }: Route.LoaderArgs) {
  const lang = new URL(request.url).searchParams.get("lang") ?? "en";
  const cycle = params.cycle;
  const home = await api<Home>(request, "/v1/me/home");
  const facts = await api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?ai=false`);
  const narrative = api<ExplanationResponse>(request, `/v1/me/bills/${cycle}/explanation?lang=${lang}`);
  const bill = cycle === "current" ? null : await api<BillDetail>(request, `/v1/me/bills/${cycle}`);
  return { home, facts, narrative, bill, lang, cycle };
}

export default function BillPage({ loaderData }: Route.ComponentProps) {
  const { home, facts, narrative, bill, lang, cycle } = loaderData;
  const loc = home.subscriber.locale;
  const isCurrent = cycle === "current";
  const lines = bill?.lines ?? home.unbilled.lines;
  const cur = bill?.currency ?? home.unbilled.currency;
  return (
    <div className="space-y-6">
      <Link to="/" className="text-sm text-[var(--ink-3)]">← Overview</Link>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold">{isCurrent ? `Unbilled usage · ${cycleLabel(home.unbilled.cycle, loc)}` : `Bill · ${cycleLabel(cycle, loc)}`}</h1>
        {bill && <div className="flex items-center gap-3">
          <Pill tone={bill.outstanding > 0 ? "warn" : "good"}>{bill.outstanding > 0 ? `Due ${dateLabel(bill.due_date, loc)}` : "✓ Paid"}</Pill>
          <a href={`/bills/${cycle}/pdf`} download className="rounded-xl border border-[var(--line)] px-3 py-1.5 text-sm font-semibold hover:bg-[var(--chip)]">⬇ Download PDF</a>
        </div>}
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Breakdown">
          <table className="w-full text-sm">
            <thead><tr className="text-left text-xs text-[var(--ink-3)]"><th className="pb-2 font-medium">Item</th><th className="pb-2 text-right font-medium">Qty</th><th className="pb-2 text-right font-medium">Amount</th></tr></thead>
            <tbody className="divide-y divide-[var(--line)]">
              {lines.map((l) => <tr key={l.code}><td className="py-2">{l.label}</td><td className="py-2 text-right tabular-nums text-[var(--ink-3)]">{l.quantity.toLocaleString(loc)} {l.unit}</td><td className="py-2 text-right tabular-nums">{money(l.amount, cur, loc)}</td></tr>)}
            </tbody>
            <tfoot className="border-t-2 border-[var(--line)]">
              <tr><td className="pt-2 text-[var(--ink-3)]">Subtotal</td><td /><td className="pt-2 text-right tabular-nums">{money(bill?.subtotal ?? home.unbilled.subtotal, cur, loc)}</td></tr>
              <tr><td className="text-[var(--ink-3)]">VAT {bill ? `${Math.round(Number(bill.vat_rate) * 100)}%` : ""}</td><td /><td className="text-right tabular-nums">{money(bill?.vat ?? home.unbilled.vat, cur, loc)}</td></tr>
              <tr className="text-base font-bold"><td className="pt-1">Total</td><td /><td className="pt-1 text-right tabular-nums">{money(bill?.total ?? home.unbilled.total, cur, loc)}</td></tr>
            </tfoot>
          </table>
          {bill && bill.outstanding > 0 && home.features.payments && (
            <div className="mt-4"><PayButton purpose="BILL" billId={bill.id} label={`Pay ${money(bill.outstanding, cur, loc)}`} name={home.subscriber.name} /></div>
          )}
        </Card>
        <div className="space-y-6">
          <Narrative narrative={narrative} lang={lang} basePath={`/bills/${cycle}`} />
          <TopDrivers ex={facts.explanation} locale={loc} />
        </div>
      </div>
      <WhyChanged ex={facts.explanation} locale={loc} title={isCurrent ? "So far vs your last bill" : undefined} />
    </div>
  );
}
