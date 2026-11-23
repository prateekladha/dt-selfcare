import { useState } from "react";
import { Check, CheckCircle2, Gauge, Loader2, MessageSquare, Phone, Smartphone } from "lucide-react";
import { api, PRIVATE } from "~/lib/api.server";
import { Card, Pill } from "~/components/ui";
import { AddDataButton, OrderCard } from "~/components/OrderCard";
import { toast } from "~/components/Toast";
import { mbToGb, money } from "~/lib/format";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/plan";

export const meta = () => [{ title: "My plan · Self-care" }];
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
  const { home } = useSnapshot();
  const loc = home.subscriber.locale;
  const mine = plans.find((p) => p.code === current);
  const addOns = home.unbilled.lines.filter((l) => l.category === "ADDON");
  const used = home.usage;
  const [busy, setBusy] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);

  async function choose(code: string) {
    setBusy(code);
    setProposal(null);   // a new choice replaces an earlier (declined / expired / completed) proposal
    try {
      const r = await fetch("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_code: code }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok) setProposal(j); else toast(j.detail ?? "Couldn't prepare the tariff change", "bad");
    } catch {
      toast("We can't reach the service right now. Please try again in a moment.", "bad");
    } finally {
      setBusy(null);   // never leave every button disabled
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold">My plan</h1>
        <p className="mt-1 text-[15px] text-[var(--ink-3)]">Your active tariff, what it includes and how much you've used this month.</p>
      </div>

      {mine && (
        <Card className="overflow-hidden border-[var(--brand)] ring-1 ring-[var(--brand)]">
          <div className="flex flex-wrap items-start justify-between gap-4 bg-[var(--brand-bg)] px-6 py-5">
            <div className="flex items-start gap-3">
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-[var(--brand)] text-white"><Smartphone size={20} /></span>
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-[var(--brand-ink)]">Your current plan</p>
                <h2 className="display mt-0.5 text-[26px] font-extrabold leading-tight">{mine.name}</h2>
              </div>
            </div>
            <div className="w-full sm:w-auto sm:text-right">
              <Pill tone="good" icon={<CheckCircle2 size={13} />}>Active</Pill>
              <p className="mt-2 text-[22px] font-extrabold leading-none">{money(mine.monthly_fee, currency, loc)}<span className="text-sm font-medium text-[var(--ink-3)]"> / month + VAT</span></p>
            </div>
          </div>
          <div className="grid gap-4 px-6 py-5 sm:grid-cols-3">
            <div className="flex items-start gap-3"><Gauge size={18} className="mt-0.5 text-[var(--ink-3)]" />
              <div><p className="text-sm font-semibold">{gb(mine.data_mb)} data{addOns.length > 0 && " + add-ons"}</p>
                <p className="text-[13px] text-[var(--ink-3)]">{mbToGb(used.data_mb)} of {mbToGb(used.data_allowance_mb)} GB used this month</p>
                <p className="text-[13px] text-[var(--ink-3)]">{mine.overage_policy === "THROTTLE" ? "Then slower speed, no extra charge" : "Then extra data charged per MB"}</p></div></div>
            <div className="flex items-start gap-3"><Phone size={18} className="mt-0.5 text-[var(--ink-3)]" />
              <div><p className="text-sm font-semibold">{mine.voice_min == null ? "Unlimited calls" : `${mine.voice_min} minutes`}</p>
                <p className="text-[13px] text-[var(--ink-3)]">{used.voice_min} min used this month</p></div></div>
            <div className="flex items-start gap-3"><MessageSquare size={18} className="mt-0.5 text-[var(--ink-3)]" />
              <div><p className="text-sm font-semibold">{mine.sms == null ? "Unlimited SMS" : `${mine.sms} SMS`}</p>
                <p className="text-[13px] text-[var(--ink-3)]">{used.sms} SMS used this month</p></div></div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] px-6 py-4">
            <p className="text-[13px] text-[var(--ink-3)]">{addOns.length
              ? <>Add-ons this month: {addOns.map((a) => `${a.label}${a.quantity > 1 ? ` × ${a.quantity}` : ""}`).join(", ")}</>
              : "No add-ons this month."}</p>
            <AddDataButton country={home.subscriber.country} />
          </div>
        </Card>
      )}

      <div>
        <h2 className="display text-[22px] font-extrabold">Change tariff</h2>
        <p className="mt-1 text-[15px] text-[var(--ink-3)]">Pick a tariff. Nothing changes until you approve; the switch is then confirmed by the network. The month you switch is pro-rated by day.</p>
      </div>

      {proposal && (
        <OrderCard kind="PLAN" orderId={proposal.order_id} title={`${proposal.preview.from} → ${proposal.preview.name}`}
                   extra={`Data ${proposal.preview.old_data} → ${proposal.preview.data}`}
                   price={`${money(proposal.preview.monthly_fee, proposal.preview.currency)} / month + VAT (this month ${money(proposal.preview.this_month_fee, proposal.preview.currency)}: ${proposal.preview.days_old} days old tariff, ${proposal.preview.days_new} days new)`} />
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[...plans].sort((a, b) => Number(b.code === current) - Number(a.code === current)).map((p) => {
          const isCurrent = p.code === current;
          return (
            <Card key={p.code} className={`flex flex-col p-5 ${isCurrent ? "!border-[var(--brand)] !bg-[var(--brand-bg)] ring-1 ring-[var(--brand)]" : ""}`}>
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-lg font-bold">{p.name}</h3>
                {isCurrent && <Pill tone="brand" icon={<Check size={13} />}>Current plan</Pill>}
              </div>
              <p className="mt-2 text-[28px] font-extrabold leading-none">{money(p.monthly_fee, currency, loc)}<span className="text-sm font-medium text-[var(--ink-3)]"> / month + VAT</span></p>
              <ul className="mt-4 space-y-1.5 text-[14px] text-[var(--ink-2)]">
                <li>{gb(p.data_mb)} data</li>
                <li>{p.voice_min == null ? "Unlimited calls" : `${p.voice_min} minutes`}</li>
                <li>{p.sms == null ? "Unlimited SMS" : `${p.sms} SMS`}</li>
                <li>{p.overage_policy === "THROTTLE" ? "Slower speed after your data, no extra charge" : "Extra data charged per MB"}</li>
              </ul>
              <div className="mt-auto pt-5">
                {isCurrent ? (
                  <p className="flex h-10 items-center justify-center gap-1.5 rounded-lg border border-[var(--brand)]/40 text-sm font-semibold text-[var(--brand-ink)]"><CheckCircle2 size={15} /> You're on this plan</p>
                ) : (
                  <button type="button" onClick={() => choose(p.code)} disabled={busy !== null}
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
