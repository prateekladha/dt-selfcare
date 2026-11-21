import { Form, useNavigation } from "react-router";
import { Narrative, TopDrivers, WhyChanged } from "~/components/Explain";
import { Card } from "~/components/ui";
import { api, ApiError, PRIVATE } from "~/lib/api.server";
import { money } from "~/lib/format";
import type { Explanation, Home, Line } from "~/lib/types";
import type { Route } from "./+types/insights";

export const headers = () => PRIVATE;
export const meta = () => [{ title: "Try your usage · Mobile self-care" }];

type SimResult = { source: string; compared_to: string | null; narrative: string; provider: string;
  bill: { lines: Line[]; subtotal: number; vat: number; total: number; currency: string }; explanation: Explanation };

export async function loader({ request }: Route.LoaderArgs) {
  const [home, scenarios] = await Promise.all([
    api<Home>(request, "/v1/me/home"),
    api<{ key: string; title: string }[]>(request, "/v1/me/usage/scenarios"),
  ]);
  return { locale: home.subscriber.locale, plan: home.plan.name, scenarios };
}

export async function action({ request }: Route.ActionArgs) {
  const form = await request.formData();
  const lang = String(form.get("lang") ?? "en");
  const body = new FormData();
  const file = form.get("file");
  if (file instanceof File && file.size > 0) body.set("file", file, file.name);
  else if (form.get("scenario")) body.set("scenario", String(form.get("scenario")));
  else return { error: "Choose a scenario or upload a file." };
  try {
    return { result: await api<SimResult>(request, `/v1/me/usage/simulate?lang=${lang}`, { method: "POST", body }) };
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    throw e;
  }
}

export default function Insights({ loaderData, actionData }: Route.ComponentProps) {
  const { locale, plan, scenarios } = loaderData;
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const r = actionData && "result" in actionData ? actionData.result : null;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Try your usage</h1>
        <p className="text-sm text-[var(--ink-3)]">Upload a usage file or pick a scenario: we price it on your plan ({plan}) and explain it against your last bill. This never changes your real bill.</p>
      </div>
      <div className="grid gap-6 md:grid-cols-2">
        <Card title="Pick a scenario">
          <Form method="post" className="grid gap-2 sm:grid-cols-2">
            {scenarios.map((s) => (
              <button key={s.key} name="scenario" value={s.key} disabled={busy}
                      className="rounded-xl border border-[var(--line)] px-3 py-3 text-left text-sm font-medium hover:border-[var(--brand)] hover:bg-[var(--brand-bg)] disabled:opacity-60">{s.title}</button>
            ))}
          </Form>
        </Card>
        <Card title="Upload usage (CSV or JSON)">
          <Form method="post" encType="multipart/form-data" className="space-y-3">
            <input type="file" name="file" accept=".csv,.json" required
                   className="block w-full text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-[var(--brand-bg)] file:px-3 file:py-2 file:font-semibold file:text-[var(--brand-ink)]" />
            <div className="flex items-center justify-between">
              <a href="/api/sample.csv" className="text-xs font-semibold text-[var(--brand-ink)]" download="usage-sample.csv">Download a sample file</a>
              <button disabled={busy} className="rounded-xl bg-[var(--brand)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-60">{busy ? "Analysing…" : "Analyse"}</button>
            </div>
            <p className="text-xs text-[var(--ink-3)]">Columns: usage_type (DATA, VOICE, SMS, ROAMING_DATA, ROAMING_VOICE, INTL_VOICE), quantity, unit (GB/MB, min, count), roaming_zone (EU, Z2, Z3).</p>
          </Form>
        </Card>
      </div>
      {actionData && "error" in actionData && <p className="rounded-xl bg-[var(--bad-bg)] p-3 text-sm text-[var(--bad)]">{actionData.error}</p>}
      {r && (
        <div className="space-y-6">
          <Card title={`Estimated bill · ${r.source.replace(/^(scenario|upload):/, "")}`}>
            <p className="text-3xl font-bold">{money(r.bill.total, r.bill.currency, locale)}</p>
            <ul className="mt-3 space-y-1 text-sm">{r.bill.lines.map((l) => (
              <li key={l.code} className="flex justify-between"><span className="text-[var(--ink-2)]">{l.label} · {l.quantity.toLocaleString(locale)} {l.unit}</span><span className="tabular-nums">{money(l.amount, r.bill.currency, locale)}</span></li>))}
            </ul>
          </Card>
          <div className="grid gap-6 lg:grid-cols-2">
            <WhyChanged ex={r.explanation} locale={locale} title={r.compared_to ? "Compared with your last bill" : "Your estimate"} />
            <div className="space-y-6">
              <Narrative narrative={{ narrative: r.narrative, provider: r.provider, explanation: r.explanation, partial: false }} lang="en" basePath="/insights" />
              <TopDrivers ex={r.explanation} locale={locale} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
