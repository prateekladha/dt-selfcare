import { ArrowLeft, CheckCircle2, Loader2, Lock } from "lucide-react";
import { Link } from "react-router";
import { PaymentsDown } from "~/components/PayLink";
import { Button, Card, LinkButton, Pill } from "~/components/ui";
import { ApiError, api, PRIVATE } from "~/lib/api.server";
import { useCheckout } from "~/lib/checkout";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import type { Line } from "~/lib/types";
import { useDegraded, useSnapshot } from "./_app";
import type { Route } from "./+types/pay";

export const headers = () => PRIVATE;
export const meta = ({ params }: Route.MetaArgs) => [{ title: `${params.cycle ? "Pay your bill" : "Pay in advance"} · Self-care` }];

type BillDetail = { id: string; cycle: string; total: number; subtotal: number; vat: number; vat_rate: string; currency: string;
  status: string; outstanding: number; due_date: string; lines: Line[] };

/** /pay/bill/:cycle (an issued bill) and /pay/advance (this month's unbilled usage): what is being paid,
 *  then one button that opens the gateway's secure checkout. */
export async function loader({ request, params }: Route.LoaderArgs) {
  const cycle = params.cycle;
  if (cycle === undefined) return { bill: null };
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(cycle)) throw new Response("Not found", { status: 404 });  // never forwarded raw
  try {
    return { bill: await api<BillDetail>(request, `/v1/me/bills/${cycle}`) };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) throw new Response("Not found", { status: 404 });
    throw e;
  }
}

export default function PayPage({ loaderData }: Route.ComponentProps) {
  const { bill } = loaderData;
  const { home } = useSnapshot();
  const loc = home.subscriber.locale;
  const paymentsDown = useDegraded("payments");
  const purpose = bill ? "BILL" : "ADVANCE";
  const { start, busy } = useCheckout(purpose, bill?.id ?? null,
    { name: home.subscriber.name, contact: `+${home.subscriber.id}`, email: home.subscriber.email });

  const cur = bill?.currency ?? home.unbilled.currency;
  const amount = bill ? bill.outstanding : home.unbilled.total;
  const lines = bill?.lines ?? home.unbilled.lines;
  const subtotal = bill?.subtotal ?? home.unbilled.subtotal;
  const vat = bill?.vat ?? home.unbilled.vat;
  const total = bill?.total ?? home.unbilled.total;
  const alreadyPaid = bill ? bill.total - bill.outstanding : 0;
  const overdue = !!bill && amount > 0 && new Date(bill.due_date) < new Date(home.as_of);
  const month = cycleLabel(bill?.cycle ?? home.unbilled.cycle, loc);
  const back = bill ? `/bills/${bill.cycle}` : "/bills/current";

  return (
    <div className="mx-auto max-w-[560px] space-y-6">
      <Link to={back} className="inline-flex items-center gap-1.5 text-sm font-semibold text-[var(--ink-3)] hover:text-[var(--ink)]"><ArrowLeft size={16} /> {bill ? `${month} bill` : "This month"}</Link>
      <div>
        <p className="text-sm font-semibold uppercase tracking-wide text-[var(--ink-3)]">Payment</p>
        <h1 className="display mt-1 text-[32px] font-extrabold leading-tight">{bill ? `Pay your ${month} bill` : `Pay ${month} in advance`}</h1>
        {!bill && <p className="mt-2 text-[15px] text-[var(--ink-3)]">Usage so far this month. Paying now is credited to this month's bill when it's issued.</p>}
      </div>

      <Card>
        <div className="px-6 pb-5 pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-[var(--ink-3)]">{amount > 0 ? "To pay, including VAT" : "Nothing to pay"}</p>
            {bill && (amount <= 0 ? <Pill tone="good" icon={<CheckCircle2 size={13} />}>Paid</Pill>
              : <Pill tone={overdue ? "bad" : "warn"}>{overdue ? "Overdue · due" : "Due"} {dateLabel(bill.due_date, loc)}</Pill>)}
          </div>
          <p className="display tabular mt-1 text-[44px] font-extrabold leading-none" data-testid="pay-amount">{money(Math.max(amount, 0), cur, loc)}</p>
        </div>
        <ul className="space-y-2 border-t border-[var(--line)] px-6 py-4 text-[14px]">
          {lines.map((l) => (
            <li key={l.code} className="flex justify-between gap-4"><span className="text-[var(--ink-2)]">{l.label}</span><span className="tabular">{money(l.amount, cur, loc)}</span></li>
          ))}
          <li className="flex justify-between gap-4 border-t border-[var(--line)] pt-2 text-[var(--ink-3)]"><span>Net</span><span className="tabular">{money(subtotal, cur, loc)}</span></li>
          <li className="flex justify-between gap-4 text-[var(--ink-3)]"><span>VAT</span><span className="tabular">{money(vat, cur, loc)}</span></li>
          <li className="flex justify-between gap-4 font-semibold"><span>Total</span><span className="tabular">{money(total, cur, loc)}</span></li>
          {alreadyPaid > 0 && <li className="flex justify-between gap-4 text-[var(--ink-3)]"><span>Already paid</span><span className="tabular">−{money(alreadyPaid, cur, loc)}</span></li>}
        </ul>
        <div className="border-t border-[var(--line)] px-6 py-5">
          {amount <= 0 ? (
            <div className="flex flex-wrap gap-2"><LinkButton to={back} variant="secondary">{bill ? "View bill" : "View this month"}</LinkButton><LinkButton to="/" variant="ghost">Back to overview</LinkButton></div>
          ) : !home.features.payments ? (
            <p className="text-[14px] text-[var(--ink-2)]">Online payment isn't available for your account right now.</p>
          ) : paymentsDown ? <PaymentsDown /> : (
            <>
              <Button onClick={start} disabled={busy} aria-busy={busy} className="w-full">
                {busy ? <Loader2 size={16} className="animate-spin" /> : <Lock size={16} />}
                <span>Pay securely {money(amount, cur, loc)}</span>
              </Button>
              <p className="mt-3 flex items-start gap-1.5 text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                <Lock size={13} className="mt-0.5 shrink-0" />
                Opens Razorpay's secure checkout. Your card or UPI details go only to Razorpay; we never see them. Paying twice by accident isn't possible: a retry reuses the same order.
              </p>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
