import { CheckCircle2, Clock, Download, XCircle } from "lucide-react";
import { useEffect, useRef } from "react";
import { useRevalidator } from "react-router";
import { Card, LinkButton } from "~/components/ui";
import { ApiError, api, PRIVATE } from "~/lib/api.server";
import { cycleLabel, money } from "~/lib/format";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/pay-result";

export const headers = () => PRIVATE;
export const meta = () => [{ title: "Payment · Self-care" }];

type Payment = { payment_id: string; status: "CREATED" | "PENDING" | "PAID" | "FAILED" | "EXPIRED"; purpose: "BILL" | "ADVANCE";
  cycle: string; amount: number; currency: string; charged_amount: number; charged_currency: string; fx_rate: string | null;
  gateway: string; gateway_payment_id: string | null; created_at: string };

/** /pay/result?id=<payment>: the outcome, taken from the server. The loader asks core-api with
 *  refresh=true, i.e. the gateway's status API is queried unless the payment is already PAID; the
 *  browser's success callback is never the source of truth. Reloading this page simply re-checks. */
export async function loader({ request }: Route.LoaderArgs) {
  const id = new URL(request.url).searchParams.get("id") ?? "";
  if (!/^[A-Za-z0-9-]{8,64}$/.test(id)) throw new Response("Not found", { status: 404 });
  try {
    return { p: await api<Payment>(request, `/v1/me/payments/${id}?refresh=true`) };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) throw new Response("Not found", { status: 404 });
    throw e;
  }
}

const POLL_MS = 4000, POLL_MAX = 15;   // up to a minute for a bank confirmation (UPI can take a while)

export default function PayResult({ loaderData }: Route.ComponentProps) {
  const { p } = loaderData;
  const { home, d } = useSnapshot();
  const loc = home.subscriber.locale;
  const revalidator = useRevalidator();
  const polls = useRef(0);
  const waiting = p.status === "PENDING" || p.status === "CREATED";
  useEffect(() => {
    if (!waiting || polls.current >= POLL_MAX) return;
    const t = setTimeout(() => { polls.current += 1; revalidator.revalidate(); }, POLL_MS);
    return () => clearTimeout(t);
  }, [waiting, p, revalidator]);

  const month = cycleLabel(p.cycle, loc);
  const what = p.purpose === "BILL" ? `${month} bill` : `${month} usage, paid in advance`;
  const billLink = p.purpose === "BILL" ? `/bills/${p.cycle}` : "/bills/current";
  const retry = p.purpose === "BILL" ? `/pay/bill/${p.cycle}` : "/pay/advance";
  const fx = p.charged_currency !== p.currency;
  const gaveUp = waiting && polls.current >= POLL_MAX;

  const view = p.status === "PAID"
    ? { icon: <CheckCircle2 size={28} />, tone: "text-[var(--good)]", title: "Payment received", body: `Thank you. Your ${what} is paid.` }
    : p.status === "FAILED" || p.status === "EXPIRED"
      ? { icon: <XCircle size={28} />, tone: "text-[var(--bad)]", title: "Payment didn't go through", body: "Nothing was charged. You can try again with another card or UPI app." }
      : { icon: <Clock size={28} />, tone: "text-[var(--warn)]", title: "Confirming your payment",
          body: gaveUp ? "Your bank hasn't confirmed it yet. You don't need to pay again: we keep checking and update your bill as soon as it's confirmed."
                       : "We're waiting for your bank to confirm. This page updates by itself; please don't pay again." };

  return (
    <div className="mx-auto max-w-[560px] space-y-6">
      <Card>
        <div className="px-6 pb-6 pt-7 text-center">
          <span className={`mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-[var(--chip)] ${view.tone}`}>{view.icon}</span>
          <h1 className="display mt-4 text-[28px] font-extrabold leading-tight" data-testid="pay-status">{view.title}</h1>
          <p className="mx-auto mt-2 max-w-[400px] text-[15px] leading-relaxed text-[var(--ink-3)]">{view.body}</p>
          <p className="display tabular mt-5 text-[40px] font-extrabold leading-none">{money(p.amount, p.currency, loc)}</p>
          {fx && <p className="mt-2 text-[13px] text-[var(--ink-3)]">Charged as {money(p.charged_amount, p.charged_currency, loc)} (1 {p.currency} = {p.fx_rate} {p.charged_currency})</p>}
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 border-t border-[var(--line)] px-6 py-4 text-[14px]">
          <dt className="text-[var(--ink-3)]">For</dt><dd className="text-right">{what}</dd>
          <dt className="text-[var(--ink-3)]">Date</dt><dd className="text-right">{new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: d.tz }).format(new Date(p.created_at))}</dd>
          {p.gateway_payment_id && <><dt className="text-[var(--ink-3)]">Reference</dt><dd className="break-all text-right font-mono text-[13px]">{p.gateway_payment_id}</dd></>}
          <dt className="text-[var(--ink-3)]">Paid with</dt><dd className="text-right capitalize">{p.gateway}</dd>
        </dl>
        <div className="flex flex-wrap justify-center gap-2 border-t border-[var(--line)] px-6 py-5">
          {p.status === "PAID" ? (<>
            <LinkButton to={billLink} variant="primary">View bill</LinkButton>
            {p.purpose === "BILL" && <LinkButton to={`/bills/${p.cycle}/pdf`} variant="secondary" download><Download size={16} /> PDF</LinkButton>}
            <LinkButton to="/" variant="ghost">Back to overview</LinkButton>
          </>) : p.status === "FAILED" || p.status === "EXPIRED" ? (<>
            <LinkButton to={retry} variant="primary">Try again</LinkButton>
            <LinkButton to="/" variant="ghost">Back to overview</LinkButton>
          </>) : (
            <LinkButton to="/" variant="ghost">Back to overview</LinkButton>
          )}
        </div>
      </Card>
    </div>
  );
}
