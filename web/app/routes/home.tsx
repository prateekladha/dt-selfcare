import { Link } from "react-router";
import { Narrative, TopDrivers, WhyChanged } from "~/components/Explain";
import { PayButton } from "~/components/PayButton";
import { TrendChart } from "~/components/TrendChart";
import { UsageMeter } from "~/components/UsageMeter";
import { Card, Pill } from "~/components/ui";
import { api, PRIVATE } from "~/lib/api.server";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import type { ExplanationResponse, Home } from "~/lib/types";
import type { Route } from "./+types/home";

export const meta = () => [{ title: "Overview · Mobile self-care" }];
export const headers = () => PRIVATE;

// SSR loader: ONE snapshot call for every number on the page (consistent "as of" moment), plus the
// deterministic explanation. Only the AI narrative is streamed in afterwards (it may take seconds).
export async function loader({ request }: Route.LoaderArgs) {
  const lang = new URL(request.url).searchParams.get("lang") ?? "en";
  const home = await api<Home>(request, "/v1/me/home");
  const last = home.last_bill?.cycle;
  const facts = last ? await api<ExplanationResponse>(request, `/v1/me/bills/${last}/explanation?ai=false`) : null;
  const narrative = last ? api<ExplanationResponse>(request, `/v1/me/bills/${last}/explanation?lang=${lang}`) : null;
  return { home, facts, narrative, lang };
}

export default function HomePage({ loaderData }: Route.ComponentProps) {
  const { home, facts, narrative, lang } = loaderData;
  const loc = home.subscriber.locale;
  const u = home.usage;
  const cur = home.unbilled.currency;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold">Hi {home.subscriber.name.split(" ")[0]} 👋</h1>
          <p className="text-sm text-[var(--ink-3)]">+{home.subscriber.id} · {home.plan.name} ({home.plan.code}) · {home.subscriber.country}</p>
        </div>
        <p className="text-xs text-[var(--ink-3)]" title={home.as_of}>Data as of {new Date(home.as_of).toISOString().slice(11, 16)} UTC</p>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <Card title={`Data · ${cycleLabel(u.cycle, loc)}`}>
          <UsageMeter usedMb={u.data_mb} allowanceMb={u.data_allowance_mb} pct={u.data_pct} policy={home.plan.overage_policy} />
          <dl className="mt-4 grid grid-cols-3 gap-2 text-center text-xs">
            <div className="rounded-lg bg-[var(--chip)] p-2"><dt className="text-[var(--ink-3)]">Calls</dt><dd className="font-semibold">{u.voice_min} min</dd></div>
            <div className="rounded-lg bg-[var(--chip)] p-2"><dt className="text-[var(--ink-3)]">SMS</dt><dd className="font-semibold">{u.sms}</dd></div>
            <div className="rounded-lg bg-[var(--chip)] p-2"><dt className="text-[var(--ink-3)]">Roaming</dt>
              <dd className="font-semibold">{Object.values(u.roaming_data_mb).reduce((a, b) => a + b, 0) + u.eu_roaming_data_mb} MB</dd></div>
          </dl>
        </Card>

        <Card title="Unbilled so far">
          <p className="text-3xl font-bold">{money(home.unbilled.total, cur, loc)}</p>
          <p className="text-xs text-[var(--ink-3)]">{cycleLabel(home.unbilled.cycle, loc)} · incl. VAT {money(home.unbilled.vat, cur, loc)}</p>
          <ul className="mt-3 space-y-1 text-sm">
            {home.unbilled.lines.map((l) => (
              <li key={l.code} className="flex justify-between"><span className="text-[var(--ink-2)]">{l.label}</span><span className="tabular-nums">{money(l.amount, cur, loc)}</span></li>
            ))}
          </ul>
          {home.features.payments && home.unbilled.total > 0 && (
            <div className="mt-4"><PayButton purpose="ADVANCE" label={`Pay ${money(home.unbilled.total, cur, loc)} now`} name={home.subscriber.name} /></div>
          )}
          <Link to="/bills/current" className="mt-3 block text-xs font-semibold text-[var(--brand-ink)]">Explain my unbilled usage →</Link>
        </Card>

        <Card title="Bill pending">
          {home.outstanding.amount > 0 && home.last_bill ? (
            <>
              <p className="text-3xl font-bold">{money(home.outstanding.amount, home.outstanding.currency, loc)}</p>
              <p className="text-xs text-[var(--ink-3)]">{cycleLabel(home.last_bill.cycle, loc)} bill · due {dateLabel(home.last_bill.due_date, loc)}</p>
              {home.features.payments && <div className="mt-4"><PayButton purpose="BILL" billId={home.outstanding.bill_id} label="Pay bill" name={home.subscriber.name} /></div>}
            </>
          ) : (
            <><p className="text-3xl font-bold">{money(0, cur, loc)}</p><p className="mt-1"><Pill tone="good">✓ All bills paid</Pill></p></>
          )}
          {home.last_bill && (
            <div className="mt-4 flex gap-3 text-xs font-semibold">
              <Link to={`/bills/${home.last_bill.cycle}`} className="text-[var(--brand-ink)]">View bill</Link>
              <a href={`/bills/${home.last_bill.cycle}/pdf`} className="text-[var(--brand-ink)]" download>Download PDF</a>
            </div>
          )}
        </Card>
      </div>

      <Card title="Bill trend · last 12 months"><TrendChart bills={home.trend} locale={loc} /></Card>

      {facts && narrative && (
        <div className="grid gap-6 lg:grid-cols-2">
          <WhyChanged ex={facts.explanation} locale={loc} />
          <div className="space-y-6">
            <Narrative narrative={narrative} lang={lang} basePath="/" />
            <TopDrivers ex={facts.explanation} locale={loc} />
          </div>
        </div>
      )}

      <Card title="Notifications">
        {home.notifications.length === 0 ? <p className="text-sm text-[var(--ink-3)]">No alerts this cycle.</p> : (
          <ul className="divide-y divide-[var(--line)]">
            {home.notifications.map((n) => (
              <li key={n.id} className="py-2.5"><p className="text-sm font-medium">{n.title}</p><p className="text-xs text-[var(--ink-3)]">{n.body} · {dateLabel(n.created_at, loc)}</p></li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
