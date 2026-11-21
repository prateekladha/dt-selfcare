import { useDegraded } from "~/routes/_app";
import { useRef, useState } from "react";
import { useRevalidator } from "react-router";
import { CheckCircle2, CreditCard, Loader2 } from "lucide-react";
import { toast } from "./Toast";
import { Button } from "./ui";

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

/** Tap to pay. One idempotency key per button instance: double taps and retries reuse the same gateway
 *  order, so the customer can never be charged twice for one intent. The button keeps its size in every
 *  state (progress is shown inside it) and messages go to the global toast, so nothing around it moves. */
export function PayButton({ purpose, billId, label, name, contact, email, variant = "primary", full = false }: { purpose: "BILL" | "ADVANCE"; billId?: string | null; label: string; name: string; contact?: string; email?: string; variant?: "primary" | "secondary"; full?: boolean }) {
  const idem = useRef<string | null>(null);
  const paymentsDown = useDegraded("payments");
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const revalidator = useRevalidator();

  async function pay() {
    setState("busy");
    idem.current ??= crypto.randomUUID();
    try {
      const res = await fetch("/api/pay", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ purpose, bill_id: billId, idempotency_key: idem.current }) });
      const p = await res.json();
      if (!res.ok) throw new Error(p.detail ?? "Payment could not be started. Please try again.");
      if (p.status === "PAID") { setState("done"); toast("This bill is already paid.", "good"); revalidator.revalidate(); return; }
      await loadCheckout();
      const rzp = new window.Razorpay!({
        key: p.checkout.key_id, order_id: p.order_id, amount: p.charged_amount, currency: p.charged_currency,
        name: "Self-care", description: purpose === "BILL" ? "Bill payment" : "Pay unbilled usage",
        prefill: { name, ...(contact ? { contact } : {}), ...(email ? { email } : {}) },  // skips the gateway's contact prompt
        theme: { color: "#E20074" },
        handler: async (r: any) => {
          const c = await fetch("/api/pay/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ payment_id: p.payment_id, gateway_payment_id: r.razorpay_payment_id, signature: r.razorpay_signature }) });
          const out = await c.json();
          setState(out.status === "PAID" ? "done" : "idle");
          toast(out.status === "PAID" ? "Payment received, thank you!" : "Payment received; we're confirming it with the bank.", "good");
          idem.current = null;
          revalidator.revalidate();
        },
        modal: { ondismiss: () => setState((s) => (s === "done" ? s : "idle")) },
      });
      rzp.on("payment.failed", (e: any) => toast(declineText(e.error?.description), "bad"));
      rzp.open();
    } catch (e: any) {
      setState("idle");
      toast(e.message ?? "Payment could not be started. Please try again.", "bad");
    }
  }

  if (paymentsDown && state === "idle") {
    return (
      <p role="status" className="max-w-[300px] rounded-lg border border-[var(--line-strong)] bg-[var(--chip)] px-3 py-2 text-[13px] text-[var(--ink-2)]">
        Payments are briefly unavailable. Your bill and due date are not affected; please try again in a few minutes.
      </p>
    );
  }
  return (
    <Button onClick={pay} variant={variant} disabled={state !== "idle"} aria-busy={state === "busy"} className={full ? "w-full" : ""}>
      {state === "busy" ? <Loader2 size={16} className="animate-spin" /> : state === "done" ? <CheckCircle2 size={16} /> : <CreditCard size={16} />}
      <span>{state === "done" ? "Paid" : label}</span>
    </Button>
  );
}
