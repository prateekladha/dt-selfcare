import { useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { api, PRIVATE } from "~/lib/api.server";
import { Card, Pill } from "~/components/ui";
import { OrderCard } from "~/components/OrderCard";
import { money } from "~/lib/format";
import type { Route } from "./+types/plan";

export const meta = () => [{ title: "Change tariff · Self-care" }];
export const headers = () => PRIVATE;

type Plan = { code: string; name: string; monthly_fee: number; data_mb: number; voice_min: number | null; sms: number | null; overage_policy: string };
type Available = { current: string; currency: string; plans: Plan[] };
type Proposal = { order_id: string; preview: { name: string; from: string; monthly_fee: number; old_data: string; data: string; currency: string; this_month_fee: number; days_old: number; days_new: number } };

export async function loader({ request }: Route.LoaderArgs) {
  return api<Available>(request, "/v1/me/orders/plans/available");
}

const gb = (mb: number) => (mb < 0 ? "Unlimited" : `${(mb / 1000).toLocaleString("en-GB")} GB`);

export default function ChangePlan({ loaderData }: Route.ComponentProps) {
  const { current, currency, plans } = loaderData;
  const [busy, setBusy] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [err, setErr] = useState("");

  async function choose(code: string) {
    setBusy(code); setErr("");
    const r = await fetch("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_code: code }) });
    const j = await r.json();
    setBusy(null);
    if (r.ok) setProposal(j); else setErr(j.detail ?? "Couldn't prepare the tariff change");
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold">Change tariff</h1>
        <p className="mt-1 text-[15px] text-[var(--ink-3)]">Pick a tariff. Nothing changes until you approve; the switch is then confirmed by the network. The month you switch is pro-rated by day.</p>
      </div>

      {proposal && (
        <OrderCard kind="PLAN" orderId={proposal.order_id} title={`${proposal.preview.from} → ${proposal.preview.name}`}
                   extra={`Data ${proposal.preview.old_data} → ${proposal.preview.data}`}
                   price={`${money(proposal.preview.monthly_fee, proposal.preview.currency)} / month + VAT (this month ${money(proposal.preview.this_month_fee, proposal.preview.currency)}: ${proposal.preview.days_old} days old tariff, ${proposal.preview.days_new} days new)`} />
      )}
      {err && <p className="text-sm font-medium text-[var(--bad)]">{err}</p>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {plans.map((p) => {
          const isCurrent = p.code === current;
          return (
            <Card key={p.code} className={`flex flex-col p-5 ${isCurrent ? "border-[var(--brand)]" : ""}`}>
              <div className="flex items-start justify-between gap-2">
                <h2 className="text-lg font-bold">{p.name}</h2>
                {isCurrent && <Pill tone="brand" icon={<Check size={13} />}>Your tariff</Pill>}
              </div>
              <p className="mt-2 text-[28px] font-extrabold leading-none">{money(p.monthly_fee, currency)}<span className="text-sm font-medium text-[var(--ink-3)]"> / month</span></p>
              <ul className="mt-4 space-y-1.5 text-[14px] text-[var(--ink-2)]">
                <li>{gb(p.data_mb)} data</li>
                <li>{p.voice_min == null ? "Unlimited calls" : `${p.voice_min} minutes`}</li>
                <li>{p.sms == null ? "Unlimited SMS" : `${p.sms} SMS`}</li>
                <li>{p.overage_policy === "THROTTLE" ? "Slower speed after your data, no extra charge" : "Extra data charged per MB"}</li>
              </ul>
              <div className="mt-auto pt-5">
                {!isCurrent && (
                  <button type="button" onClick={() => choose(p.code)} disabled={busy !== null || proposal !== null}
                          className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-[var(--brand)] px-4 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50">
                    {busy === p.code && <Loader2 size={15} className="animate-spin" />} Switch to {p.name}
                  </button>
                )}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
