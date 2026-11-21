import { useRef, useState } from "react";
import { useRevalidator } from "react-router";
import { CheckCircle2, CreditCard, Loader2 } from "lucide-react";
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

/** Tap to pay. One idempotency key per button instance: double taps and retries reuse the
 * same gateway order, so the customer can never be charged twice for one intent. */
export function PayButton({ purpose, billId, label, name, variant = "primary", full = false }: { purpose: "BILL" | "ADVANCE"; billId?: string | null; label: string; name: string; variant?: "primary" | "secondary"; full?: boolean }) {
  const idem = useRef<string | null>(null);
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [msg, setMsg] = useState("");
  const revalidator = useRevalidator();

  async function pay() {
    setState("busy"); setMsg("");
    idem.current ??= crypto.randomUUID();
    try {
      const res = await fetch("/api/pay", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ purpose, bill_id: billId, idempotency_key: idem.current }) });
      const p = await res.json();
      if (!res.ok) throw new Error(p.detail ?? "payment could not be started");
      if (p.status === "PAID") { setState("done"); revalidator.revalidate(); return; }
      if (p.fx_rate) {
        const fmt = (v: number, c: string) => new Intl.NumberFormat("en-GB", { style: "currency", currency: c }).format(v / 100);
        setMsg(`Charged as ${fmt(p.charged_amount, p.charged_currency)} (1 ${p.currency} = ${p.fx_rate} ${p.charged_currency}) via UPI/cards`);
      }
      await loadCheckout();
      const rzp = new window.Razorpay!({
        key: p.checkout.key_id, order_id: p.order_id, amount: p.charged_amount, currency: p.charged_currency,
        name: "Self-care", description: purpose === "BILL" ? "Bill payment" : "Pay unbilled usage",
        prefill: { name }, theme: { color: "#E20074" },
        handler: async (r: any) => {
          const c = await fetch("/api/pay/confirm", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ payment_id: p.payment_id, gateway_payment_id: r.razorpay_payment_id, signature: r.razorpay_signature }) });
          const out = await c.json();
          setState(out.status === "PAID" ? "done" : "idle");
          setMsg(out.status === "PAID" ? "Payment received, thank you!" : "Payment is being confirmed…");
          idem.current = null;
          revalidator.revalidate();
        },
        modal: { ondismiss: () => setState("idle") },
      });
      rzp.on("payment.failed", (e: any) => { setState("error"); setMsg(e.error?.description ?? "Payment failed"); });
      rzp.open();
    } catch (e: any) {
      setState("error"); setMsg(e.message);
    }
  }

  return (
    <div className={`flex flex-col gap-1.5 ${full ? "items-stretch" : "items-start"}`}>
      <Button onClick={pay} variant={variant} disabled={state === "busy" || state === "done"} className={full ? "w-full" : ""}>
        {state === "busy" ? <><Loader2 size={16} className="animate-spin" /> Opening checkout…</> : state === "done" ? <><CheckCircle2 size={16} /> Paid</> : <><CreditCard size={16} /> {label}</>}
      </Button>
      {msg && <p className={`text-xs ${state === "error" ? "text-[var(--bad)]" : state === "done" ? "text-[var(--good)]" : "text-[var(--ink-3)]"}`}>{msg}</p>}
    </div>
  );
}
