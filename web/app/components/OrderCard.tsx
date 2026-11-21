import { useEffect, useState } from "react";
import { ArrowLeftRight, CheckCircle2, Clock, Loader2, PackagePlus, XCircle } from "lucide-react";
import { useRevalidator } from "react-router";
import { Button } from "./ui";

type Order = { order_id: string; status: string; detail?: string; workflow?: { stage: string } | null };

/** Human-in-the-loop approval for a write action. Approve -> durable workflow; progress is polled live. */
export function OrderCard({ orderId, title, extra, price, kind = "ADDON" }:
  { orderId: string; title: string; extra: string; price: string; kind?: "ADDON" | "PLAN" }) {
  const plan = kind === "PLAN";
  const [order, setOrder] = useState<Order>({ order_id: orderId, status: "PROPOSED" });
  const [busy, setBusy] = useState(false);
  const revalidator = useRevalidator();
  const active = order.status === "APPROVED" || order.status === "SUBMITTED";

  useEffect(() => {
    if (!active) return;
    const t = setInterval(async () => {
      const r = await fetch(`/api/orders?id=${orderId}`);
      if (r.ok) {
        const o = await r.json();
        setOrder(o);
        if (o.status === "COMPLETED") revalidator.revalidate();
      }
    }, 3000);
    return () => clearInterval(t);
  }, [active, orderId]);

  async function act(op: "approve" | "decline") {
    setBusy(true);
    const r = await fetch(`/api/orders?id=${orderId}&op=${op}`, { method: "POST" });
    if (r.ok) setOrder(await r.json());
    setBusy(false);
  }

  return (
    <div className="rounded-[var(--radius)] border border-[var(--line-strong)] bg-[var(--surface)] p-4">
      <div className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--brand-bg)] text-[var(--brand-ink)]">{plan ? <ArrowLeftRight size={18} /> : <PackagePlus size={18} />}</span>
        <div className="min-w-0 flex-1">
          <p className="text-[15px] font-semibold">{title}</p>
          <p className="mt-0.5 text-[13.5px] text-[var(--ink-3)]">{plan ? <>{extra} · {price} · active from today, this month billed on the new tariff</> : <>{extra} for this billing cycle · {price}, added to your next bill</>}</p>
        </div>
      </div>
      <div className="mt-4">
        {order.status === "PROPOSED" && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => act("approve")} disabled={busy}>{busy ? <Loader2 size={15} className="animate-spin" /> : null} {plan ? "Approve tariff change" : "Approve and buy"}</Button>
            <Button size="sm" variant="secondary" onClick={() => act("decline")} disabled={busy}>Not now</Button>
          </div>
        )}
        {active && order.detail?.startsWith("Queued") && (
          <p className="flex items-start gap-2 text-[13.5px] text-[var(--ink-2)]"><Clock size={15} className="mt-0.5 shrink-0 text-[var(--warn)]" />{order.detail}</p>
        )}
        {active && !order.detail?.startsWith("Queued") && (
          <p className="flex items-center gap-2 text-[13.5px] text-[var(--ink-2)]"><Loader2 size={15} className="animate-spin" />
            {order.workflow?.stage ? `${order.workflow.stage[0].toUpperCase()}${order.workflow.stage.slice(1)}…` : "Processing…"}</p>
        )}
        {order.status === "COMPLETED" && <p className="flex items-center gap-2 text-[13.5px] font-medium text-[var(--good)]"><CheckCircle2 size={16} /> {order.detail}</p>}
        {order.status === "FAILED" && <p className="flex items-center gap-2 text-[13.5px] font-medium text-[var(--bad)]"><XCircle size={16} /> {order.detail}</p>}
        {order.status === "DECLINED" && <p className="text-[13.5px] text-[var(--ink-3)]">{plan ? "Tariff unchanged." : "Not bought."}</p>}
      </div>
    </div>
  );
}

/** "Add data" from the app: proposes the country's add-on, then shows the approval card. */
export function AddDataButton({ country }: { country: string }) {
  const [proposal, setProposal] = useState<null | { order_id: string; preview: { name: string; data_mb: number; price: number; currency: string } }>(null);
  const [err, setErr] = useState("");
  async function propose() {
    setErr("");
    const r = await fetch("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ addon_code: country === "PL" ? "PL-DATA-5GB" : "DE-DATA-5GB" }) });
    const j = await r.json();
    if (r.ok) setProposal(j); else setErr(j.detail ?? "Couldn't prepare the add-on");
  }
  if (proposal) {
    const p = proposal.preview;
    const price = new Intl.NumberFormat("en-GB", { style: "currency", currency: p.currency }).format(p.price / 100);
    return <OrderCard orderId={proposal.order_id} title={p.name} extra={`+${(p.data_mb / 1000).toFixed(0)} GB`} price={`${price} + VAT`} />;
  }
  return (
    <div>
      <Button size="sm" variant="secondary" onClick={propose}><PackagePlus size={15} /> Add data</Button>
      {err && <p className="mt-1 text-xs text-[var(--bad)]">{err}</p>}
    </div>
  );
}
