import { useEffect } from "react";
import { useRevalidator } from "react-router";
import { ArrowDown, ArrowRight, Boxes, BrainCircuit, Cloud, CreditCard, Database, ExternalLink, FileArchive, Flag, Globe, Layers, Radio, Server, ShieldCheck, Smartphone, Zap } from "lucide-react";
import { Card, CardHeader, Pill } from "~/components/ui";
import { api, PRIVATE } from "~/lib/api.server";
import type { Route } from "./+types/system";

export const headers = () => PRIVATE;
export const meta = () => [{ title: "System · Self-care" }];

type Part = { partition: number; earliest: number; end: number; applied: number | null; lag: number; outbox_depth: number };
type Overview = {
  as_of: string; runtime: { pod: string; version: string; region: string; cluster: string };
  stream: { engine?: string; usage_topic?: string; partitions?: Part[]; topics?: Record<string, { partitions: number; messages_retained: number; end: number }>; consumer_groups?: string[]; error?: string };
  valkey: { engine?: string; keys?: number; used_memory?: string; maxmemory_policy?: string; ops_per_sec?: number; aof_enabled?: boolean; uptime_s?: number; connected_clients?: number; error?: string };
  database: { engine?: string; schema_user?: string; tables?: Record<string, number>; migrations?: string[]; payments_by_status?: Record<string, number>; ai_cache_by_provider?: Record<string, number>; outbox_unpublished?: number; error?: string };
  cdr?: { by_type?: Record<string, number>; collector?: Record<string, number>; quarantine_sample?: { record_id: string | null; reason: string; batch_id: string }[]; error?: string };
  flags: Record<string, unknown>; breakers: Record<string, string>;
  ai: { providers_configured: string[]; daily_limit_per_subscriber: number }; payments: { gateway: string; webhook: string };
  you: { subscriber: string; partition: number; valkey_usage_key: string; valkey_usage_hash: Record<string, string>; valkey_profile: Record<string, string>;
         applied_offset_of_your_partition: number | null; recent_alert_events: { id: string; level: string; cycle: string; pct: string }[];
         db_bills: { id: string; cycle: string; total: number; currency: string; status: string; amount_paid: number }[];
         db_payments: { id: string; status: string; amount: number; currency: string; charged_amount: number | null; charged_currency: string | null; fx_rate: string | null; gateway: string; created_at: string }[];
         db_notifications: number };
};

export async function loader({ request }: Route.LoaderArgs) {
  return { o: await api<Overview>(request, "/v1/system/overview") };
}

const REPO = "https://github.com/prateekladha/dt-selfcare/blob/main";
const n = (v: number | undefined | null) => (v ?? 0).toLocaleString("en-GB");

function Node({ icon: Icon, title, sub, stat, tone = "neutral" }: { icon: typeof Server; title: string; sub: string; stat?: string; tone?: "neutral" | "brand" | "store" }) {
  const ring = tone === "brand" ? "border-[var(--brand)]" : tone === "store" ? "border-[var(--ink)]" : "border-[var(--line-strong)]";
  return (
    <div className={`min-w-0 flex-1 rounded-lg border bg-[var(--surface)] px-3.5 py-3 ${ring}`}>
      <div className="flex items-start gap-2"><Icon size={15} className={`mt-0.5 shrink-0 ${tone === "brand" ? "text-[var(--brand)]" : "text-[var(--ink-2)]"}`} /><p className="text-[13px] font-bold leading-tight">{title}</p></div>
      <p className="mt-1 text-xs leading-snug text-[var(--ink-3)]">{sub}</p>
      {stat && <p className="tabular mt-1.5 text-xs font-semibold leading-snug text-[var(--ink)]">{stat}</p>}
    </div>
  );
}
const Arrow = () => (<><ArrowRight size={16} className="hidden shrink-0 self-center text-[var(--ink-3)] lg:block" /><ArrowDown size={16} className="shrink-0 self-center text-[var(--ink-3)] lg:hidden" /></>);

function KV({ rows }: { rows: [string, React.ReactNode][] }) {
  return <dl className="divide-y divide-[var(--line)] text-sm">{rows.map(([k, v]) => <div key={k} className="flex justify-between gap-4 py-2"><dt className="shrink-0 text-[var(--ink-3)]">{k}</dt><dd className="tabular min-w-0 break-words text-right font-semibold">{v}</dd></div>)}</dl>;
}

function flagValue(v: unknown): string {
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (Array.isArray(o.levels)) return `${(o.levels as number[]).join(" / ")} %`;
    if (o.currency === "native") return "bill currency";
    if (o.currency && o.fx) return `${o.currency} · ${Object.entries(o.fx as Record<string, number>).map(([k, r]) => `1 ${k} = ${r}`).join(", ")}`;
    return JSON.stringify(v);
  }
  return String(v);
}

export default function System({ loaderData }: Route.ComponentProps) {
  const { o } = loaderData;
  const rv = useRevalidator();
  useEffect(() => { const t = setInterval(() => rv.state === "idle" && document.visibilityState === "visible" && rv.revalidate(), 5000); return () => clearInterval(t); }, [rv]);
  const parts = o.stream.partitions ?? [];
  const totalLag = parts.reduce((s, p) => s + p.lag, 0);
  const applied = parts.reduce((s, p) => s + (p.applied ?? 0), 0);
  const t = o.database.tables ?? {};

  return (
    <div className="min-w-0 space-y-6 overflow-x-hidden">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="display text-[34px] font-extrabold">System</h1>
          <p className="mt-1 max-w-[760px] text-[15px] text-[var(--ink-3)]">Live, read-only view of every layer behind the app, refreshed every 5 seconds. Aggregates only, plus your own data traced through each layer.</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <Pill icon={<Cloud size={12} />}>OCI {o.runtime.region}</Pill><Pill icon={<Boxes size={12} />}>{o.runtime.cluster}</Pill><Pill>build {o.runtime.version}</Pill>
        </div>
      </div>

      <Card>
        <CardHeader title="Architecture, live" subtitle="Real-time usage path (top) and customer path (bottom)"
          action={<div className="flex gap-3 text-sm"><a className="inline-flex items-center gap-1 font-semibold underline-offset-4 hover:underline" href={`${REPO}/docs/HLD.md`} target="_blank" rel="noreferrer">HLD <ExternalLink size={13} /></a>
            <a className="inline-flex items-center gap-1 font-semibold underline-offset-4 hover:underline" href={`${REPO}/docs/LLD.md`} target="_blank" rel="noreferrer">LLD <ExternalLink size={13} /></a>
            <a className="inline-flex items-center gap-1 font-semibold underline-offset-4 hover:underline" href={`${REPO}/docs/OPERATIONS.md`} target="_blank" rel="noreferrer">Runbook <ExternalLink size={13} /></a></div>} />
        <div className="space-y-5 p-6">
          <div className="flex flex-col gap-2 lg:flex-row">
            <Node icon={Radio} title="Network emulator" sub="mediation batches" stat="live, every 60 s" />
            <Arrow /><Node icon={Server} title="usage-collector" sub="validate · dedupe · gzip" stat="batch idempotency" />
            <Arrow /><Node icon={Layers} title="OCI Streaming" sub={`${o.stream.usage_topic ?? "usage-events"} · ${parts.length} partitions`} stat={`${n(applied)} msgs applied · lag ${n(totalLag)}`} tone="store" />
            <Arrow /><Node icon={Zap} title="usage-aggregator" sub="atomic Lua apply" stat="exactly-once alerts" tone="brand" />
            <Arrow /><Node icon={Database} title="Valkey" sub="running totals" stat={`${n(o.valkey.keys)} keys · ${o.valkey.used_memory ?? "?"}`} tone="store" />
            <Arrow /><Node icon={Radio} title="relay → notifier" sub="outbox → usage-alerts" stat={`${n(o.stream.topics?.["usage-alerts"]?.end)} alerts published`} />
          </div>
          <div className="flex flex-col gap-2 lg:flex-row">
            <Node icon={Smartphone} title="Browser" sub="this page" />
            <Arrow /><Node icon={Globe} title="web (SSR)" sub="React Router · Traefik TLS" />
            <Arrow /><Node icon={Server} title="core-api" sub="billing · payments · insights" stat={`pod ${o.runtime.pod.slice(-5)}`} tone="brand" />
            <Arrow /><Node icon={Database} title="Autonomous DB" sub={o.database.engine?.replace(" (Always Free)", "") ?? "Oracle"} stat={`${n(t.bills)} bills · ${n(t.payments)} payments`} tone="store" />
            <Arrow /><Node icon={BrainCircuit} title="LLM" sub={o.ai.providers_configured.length ? "configured" : "not configured"} stat={`${n(t.agent_traces)} agent runs`} />
            <Arrow /><Node icon={CreditCard} title="Razorpay" sub={o.payments.gateway} stat="webhook + server check" />
          </div>
          <div className="flex items-center gap-2 text-xs text-[var(--ink-3)]"><FileArchive size={14} /> Every usage message is also copied by <b className="text-[var(--ink-2)]">raw-archiver</b> to Object Storage (audit, disputes, reconciliation). Secrets come from OCI Vault through External Secrets; flags from flagd.</div>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader icon={<Layers size={18} />} title="Stream partitions" subtitle={o.stream.engine ?? "OCI Streaming"} action={o.stream.error ? <Pill tone="bad">unavailable</Pill> : <Pill tone={totalLag > 50 ? "warn" : "good"}>lag {n(totalLag)}</Pill>} />
          <div className="overflow-x-auto px-6 pb-6 pt-3">
            {o.stream.error ? <p className="text-sm text-[var(--bad)]">{o.stream.error}</p> : (
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-[var(--ink-3)]"><tr><th className="py-2">Partition</th><th className="py-2 text-right">Earliest</th><th className="py-2 text-right">End offset</th><th className="py-2 text-right">Applied (Valkey)</th><th className="py-2 text-right">Lag</th><th className="py-2 text-right">Alert outbox</th></tr></thead>
                <tbody className="tabular divide-y divide-[var(--line)]">
                  {parts.map((p) => (
                    <tr key={p.partition} className={p.partition === o.you.partition ? "bg-[var(--brand-bg)]" : ""}>
                      <td className="py-2 font-semibold">P{p.partition}{p.partition === o.you.partition && <span className="ml-2 text-xs font-semibold text-[var(--brand-ink)]">yours</span>}</td>
                      <td className="py-2 text-right">{n(p.earliest)}</td><td className="py-2 text-right">{n(p.end)}</td><td className="py-2 text-right">{n(p.applied)}</td>
                      <td className="py-2 text-right font-bold">{n(p.lag)}</td><td className="py-2 text-right">{n(p.outbox_depth)}</td>
                    </tr>))}
                </tbody>
              </table>)}
            <p className="mt-3 text-xs text-[var(--ink-3)]">Consumer groups: {(o.stream.consumer_groups ?? []).join(" · ")}. The aggregator stores its offset next to the state in Valkey, so a crash replay is a no-op.</p>
          </div>
        </Card>
        <Card>
          <CardHeader icon={<Database size={18} />} title="Valkey" subtitle={o.valkey.engine} />
          <div className="px-6 pb-5 pt-2"><KV rows={[["Keys", n(o.valkey.keys)], ["Memory", o.valkey.used_memory ?? "?"], ["Ops / sec", n(o.valkey.ops_per_sec)], ["Persistence", o.valkey.aof_enabled ? "AOF (fsync 1 s)" : "off"], ["Eviction", o.valkey.maxmemory_policy ?? "?"], ["Clients", n(o.valkey.connected_clients)]]} /></div>
        </Card>
      </div>

      <Card>
        <CardHeader icon={<Radio size={18} />} title="CDR coverage" subtitle="Records applied per type (atomic counters in the aggregator) and data-quality outcomes at the collector" />
        <div className="grid gap-6 p-6 pt-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {Object.entries(o.cdr?.by_type ?? {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                <div key={k} className="rounded-lg border border-[var(--line)] px-3 py-2"><p className="truncate text-[11px] font-semibold uppercase tracking-wide text-[var(--ink-3)]">{k}</p><p className="tabular text-base font-bold">{n(v)}</p></div>
              ))}
            </div>
            <p className="mt-3 text-xs text-[var(--ink-3)]">Edge cases are replayed on demand with <code>network-emulator scenarios</code> (lab line +49 151 0000 0099). See docs/CDR_SCENARIOS.md for the full matrix.</p>
          </div>
          <div>
            <KV rows={[["Quarantined (malformed / future-dated)", n(o.cdr?.collector?.quarantined)], ["Duplicates dropped", n(o.cdr?.collector?.duplicates)]]} />
            {!!o.cdr?.quarantine_sample?.length && (
              <ul className="mt-3 space-y-1 text-xs text-[var(--ink-3)]">{o.cdr.quarantine_sample.map((q, i) => <li key={i}><span className="font-mono">{q.record_id ?? "?"}</span>: {q.reason}</li>)}</ul>
            )}
          </div>
        </div>
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card>
          <CardHeader icon={<Database size={18} />} title="Autonomous Database" subtitle={`schema ${o.database.schema_user ?? ""}`} />
          <div className="px-6 pb-5 pt-2"><KV rows={[...Object.entries(t).map(([k, v]) => [k, n(v)] as [string, string]), ["outbox unpublished", n(o.database.outbox_unpublished)], ["migrations", (o.database.migrations ?? []).join(", ") || "none"]]} /></div>
        </Card>
        <Card>
          <CardHeader icon={<Flag size={18} />} title="Feature flags" subtitle="flagd · evaluated for you (country + subscriber)" />
          <div className="px-6 pb-5 pt-2"><KV rows={Object.entries(o.flags).filter(([k]) => k !== "ai-provider").map(([k, v]) => [k, flagValue(v)])} /></div>
        </Card>
        <Card>
          <CardHeader icon={<ShieldCheck size={18} />} title="Resilience & integrations" />
          <div className="px-6 pb-5 pt-2"><KV rows={[
            ...Object.entries(o.breakers).map(([k, v]) => [`breaker: ${k}`, <Pill key={k} tone={v === "open" ? "bad" : "good"}>{v}</Pill>] as [string, React.ReactNode]),
            ["LLM", o.ai.providers_configured.length ? "configured" : "not configured"],
            ["AI budget / user / day", n(o.ai.daily_limit_per_subscriber)], ["Payments", Object.entries(o.database.payments_by_status ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "none"]]} /></div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Your data, layer by layer" subtitle={`Subscriber ${o.you.subscriber}: the same facts as they exist in each store`} />
        <div className="grid gap-4 p-6 pt-4 lg:grid-cols-3 [&>*]:min-w-0">
          <div className="rounded-lg border border-[var(--line)] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">1 · Stream</p>
            <KV rows={[["Your partition", `P${o.you.partition} (crc32 % ${parts.length || 4})`], ["Applied offset", n(o.you.applied_offset_of_your_partition)], ["Profile (CRM sync)", o.you.valkey_profile.plan_code ?? "?"]]} />
            <p className="mt-3 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">Your alert events (outbox)</p>
            <ul className="mt-1 space-y-1 text-xs">{o.you.recent_alert_events.length ? o.you.recent_alert_events.map((a) => <li key={a.id} className="tabular flex justify-between"><span>{a.cycle} · {a.level}%</span><span className="text-[var(--ink-3)]">at {a.pct}% · {a.id}</span></li>) : <li className="text-[var(--ink-3)]">none</li>}</ul>
          </div>
          <div className="rounded-lg border border-[var(--line)] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">2 · Valkey</p>
            <code className="mt-1 block break-all text-[11px] text-[var(--ink-2)]">{o.you.valkey_usage_key}</code>
            <KV rows={Object.entries(o.you.valkey_usage_hash).sort().map(([k, v]) => [k, k.startsWith("alert:") ? `at offset ${v}` : Number(v).toLocaleString("en-GB")])} />
          </div>
          <div className="rounded-lg border border-[var(--line)] p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">3 · Autonomous DB</p>
            <table className="mt-1 w-full text-xs"><tbody className="tabular divide-y divide-[var(--line)]">{o.you.db_bills.map((b) => <tr key={b.id}><td className="py-1.5">bills · {b.cycle}</td><td className="py-1.5 text-right">{(b.total / 100).toFixed(2)} {b.currency}</td><td className="py-1.5 text-right">{b.status}</td></tr>)}</tbody></table>
            <table className="mt-2 w-full text-xs"><tbody className="tabular divide-y divide-[var(--line)]">{o.you.db_payments.map((p) => <tr key={p.id}><td className="py-1.5">payments · {p.status}</td><td className="py-1.5 text-right">{(p.amount / 100).toFixed(2)} {p.currency}</td><td className="py-1.5 text-right">{p.charged_currency && p.charged_currency !== p.currency ? `${((p.charged_amount ?? 0) / 100).toFixed(2)} ${p.charged_currency} @${p.fx_rate}` : p.gateway}</td></tr>)}</tbody></table>
            <p className="mt-2 text-xs text-[var(--ink-3)]">notifications: {o.you.db_notifications}</p>
          </div>
        </div>
      </Card>
    </div>
  );
}
