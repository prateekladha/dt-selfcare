import { useRef, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "~/components/Toast";
import { money } from "./format";

declare global {
  interface Window { Razorpay?: any }
}

function loadCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("could not load payment checkout"));
    document.body.appendChild(s);
  });
}

/** Gateway decline reasons a customer can act on, in plain words. */
function declineText(desc?: string): string {
  const d = (desc ?? "").toLowerCase();
  if (d.includes("international")) return "Your card was declined because it's issued abroad and international cards aren't enabled for payments yet. Please try another card.";
  if (d.includes("cancel")) return "Payment cancelled. Nothing was charged.";
  return desc ? `Payment failed: ${desc}` : "Payment failed. Nothing was charged; please try again.";
}

/** What the checkout header says. When the gateway charges another currency than the bill's (demo:
 *  INR), the customer sees both amounts and the rate, so the ₹ figure in the checkout isn't a surprise. */
function describe(purpose: "BILL" | "ADVANCE", p: { amount: number; currency: string; charged_amount: number; charged_currency: string; fx_rate?: string | null }) {
  const what = purpose === "BILL" ? "Bill payment" : "Pay unbilled usage";
  if (!p.charged_currency || p.charged_currency === p.currency) return what;
  return `${what}: ${money(p.amount, p.currency)} charged as ${money(p.charged_amount, p.charged_currency)} (1 ${p.currency} = ${p.fx_rate} ${p.charged_currency})`;
}

/** The payment page's checkout. One idempotency key per page visit: double taps and retries reuse the same
 *  gateway order, so the customer can never be charged twice for one intent. Razorpay's checkout opens
 *  as an overlay on this page (card/UPI details go only to Razorpay); afterwards we go to the result page,
 *  whose server loader asks the gateway for the real status (the browser callback is only a trigger). */
export function useCheckout(purpose: "BILL" | "ADVANCE", billId: string | null, customer: { name: string; contact?: string; email?: string }) {
  const idem = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function start() {
    setBusy(true);
    idem.current ??= crypto.randomUUID();
    try {
      const res = await fetch("/api/pay", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ purpose, bill_id: billId, idempotency_key: idem.current }) });
      const p = await res.json();
      if (!res.ok) throw new Error(p.detail ?? "Payment could not be started. Please try again.");
      if (p.status === "PAID") { navigate(`/pay/result?id=${encodeURIComponent(p.payment_id)}`); return; }
      await loadCheckout();
      const rzp = new window.Razorpay!({
        key: p.checkout.key_id, order_id: p.order_id, amount: p.charged_amount, currency: p.charged_currency,
        name: "Self-care", description: describe(purpose, p),
        prefill: { name: customer.name, ...(customer.contact ? { contact: customer.contact } : {}), ...(customer.email ? { email: customer.email } : {}) },
        theme: { color: "#E20074" },
        handler: async (r: any) => {
          // trigger only: the signature is verified and the status taken from the gateway's status API
          await fetch("/api/pay/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ payment_id: p.payment_id, gateway_payment_id: r.razorpay_payment_id, signature: r.razorpay_signature }) }).catch(() => null);
          idem.current = null;
          navigate(`/pay/result?id=${encodeURIComponent(p.payment_id)}`);
        },
        modal: { ondismiss: () => setBusy(false) },
      });
      rzp.on("payment.failed", (e: any) => toast(declineText(e.error?.description), "bad"));
      rzp.open();
    } catch (e: any) {
      setBusy(false);
      toast(e.message ?? "Payment could not be started. Please try again.", "bad");
    }
  }
  return { start, busy };
}
