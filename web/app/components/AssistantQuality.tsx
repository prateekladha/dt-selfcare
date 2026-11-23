import { Gauge } from "lucide-react";
import { Card, CardHeader, Pill } from "./ui";

type Agg = { answers: number; verified_rate: number | null; repaired_rate: number | null; unverified_rate: number | null;
  p50_ms: number | null; p95_ms: number | null; cost_per_answer_usd: number | null; judged: number; judge_pass_rate: number | null };
type Run = { kind: string; prompt_version: string; judge_version: string; passed: boolean; at: string; n?: number;
  grounded_rate?: number; mention_rate?: number; tools_rate?: number; judge_pass_rate?: number | null; judge_trusted?: boolean;
  p50_ms?: number | null; p95_ms?: number | null; cost_per_answer_usd?: number | null; failed?: string[]; violations?: string[];
  accuracy?: number; kappa?: number; fail_recall?: number | null; trusted?: boolean; unreviewed_labels?: number; disagreements?: string[] };
export type Quality = { window_days: number; judge_version: string; sample_rate: number; days: ({ day: string } & Agg)[];
  last7: Agg; prev7: Agg; violations_7d: Record<string, number>; evals: Run[]; calibration: Run | null; error?: string };

/** What each policy rule protects (the judge's rule ids). */
const RULES: Record<string, string> = {
  R1: "Tariff change applies today", R2: "Add-on billing", R3: "Proposed is not done", R4: "Overdue blocks orders",
  R5: "Beyond-allowance rule of the plan", R6: "Only real products and prices", R7: "Honest add-on effect",
  R8: "No internals, resists injection", R9: "Answers the question first",
};

const pct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);
const secs = (ms: number | null | undefined) => (ms == null ? "–" : `${(ms / 1000).toFixed(1)} s`);
const usd = (v: number | null | undefined) => (v == null ? "–" : `$${v.toFixed(4)}`);
const when = (iso: string) => new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";

function Tile({ label, value, prev, sub, better }: { label: string; value: string; prev: string; sub?: string; better?: boolean | null }) {
  return (
    <div className="min-w-0 rounded-lg bg-[var(--surface-2)] px-4 py-3 ring-1 ring-[var(--line)]">
      <p className="text-xs font-semibold text-[var(--ink-3)]">{label}</p>
      <p className="tabular mt-1 text-[24px] font-extrabold leading-none">{value}</p>
      <p className="mt-1.5 text-xs text-[var(--ink-3)]">
        {better != null && <span className={better ? "text-[var(--good)]" : "text-[var(--bad)]"}>{better ? "▲ better" : "▼ worse"} · </span>}
        prev. 7 days {prev}{sub && <> · {sub}</>}
      </p>
    </div>
  );
}

function cmp(a: number | null, b: number | null, higherIsBetter: boolean): boolean | null {
  if (a == null || b == null || Math.abs(a - b) < 1e-9) return null;
  return higherIsBetter ? a > b : a < b;
}

/** Assistant quality over time: production answers (verified / repaired, latency, cost), a sampled policy judge,
 *  and the offline eval runs with the judge's calibration against human labels. */
export function AssistantQuality({ q }: { q: Quality }) {
  if (q.error) return <Card className="p-6 text-sm text-[var(--ink-3)]">Assistant quality isn't available right now.</Card>;
  const { last7: a, prev7: b } = q;
  const days = [...q.days].reverse();
  const cal = q.calibration;
  const viol = Object.entries(q.violations_7d);
  return (
    <Card>
      <CardHeader icon={<Gauge size={18} />} title="Assistant quality"
        subtitle={`Production answers, last 7 days vs the 7 before · ${Math.round(q.sample_rate * 100)}% of answers are checked by the policy judge (${q.judge_version})`}
        action={cal ? <Pill tone={cal.trusted ? "good" : "warn"}>judge κ {cal.kappa?.toFixed(2) ?? "–"}</Pill> : <Pill>judge not calibrated</Pill>} />
      <div className="grid gap-3 px-6 pt-4 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label="Verified answers" value={pct(a.verified_rate)} prev={pct(b.verified_rate)} sub={`${a.answers} answers`} better={cmp(a.verified_rate, b.verified_rate, true)} />
        <Tile label="Policy judge pass" value={pct(a.judge_pass_rate)} prev={pct(b.judge_pass_rate)} sub={`${a.judged} judged`} better={cmp(a.judge_pass_rate, b.judge_pass_rate, true)} />
        <Tile label="Latency p95" value={secs(a.p95_ms)} prev={secs(b.p95_ms)} sub={`p50 ${secs(a.p50_ms)}`} better={cmp(a.p95_ms, b.p95_ms, false)} />
        <Tile label="Cost per answer" value={usd(a.cost_per_answer_usd)} prev={usd(b.cost_per_answer_usd)} better={cmp(a.cost_per_answer_usd, b.cost_per_answer_usd, false)} />
      </div>

      {viol.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 px-6 pt-4 text-xs">
          <span className="font-semibold text-[var(--ink-3)]">Rules broken (judge, 7 days):</span>
          {viol.map(([r, c]) => <Pill key={r} tone="bad">{r} {RULES[r] ?? ""} × {c}</Pill>)}
        </div>
      )}

      <div className="grid gap-6 p-6 pt-5 lg:grid-cols-5 [&>*]:min-w-0">
        <div className="lg:col-span-3">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">Per day</p>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-xs">
              <thead className="text-left text-[var(--ink-3)]"><tr>
                <th className="py-1.5 font-semibold">Day</th><th className="py-1.5 text-right font-semibold">Answers</th>
                <th className="py-1.5 text-right font-semibold">Verified</th><th className="py-1.5 text-right font-semibold">Repaired</th>
                <th className="py-1.5 text-right font-semibold">Judge pass</th><th className="py-1.5 text-right font-semibold">p50 / p95</th>
                <th className="py-1.5 text-right font-semibold">Cost / answer</th></tr></thead>
              <tbody className="tabular divide-y divide-[var(--line)]">
                {days.map((d) => (
                  <tr key={d.day} className={d.answers ? "" : "text-[var(--ink-3)]"}>
                    <td className="py-1.5">{new Date(`${d.day}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })}</td>
                    <td className="py-1.5 text-right">{d.answers || "–"}</td>
                    <td className="py-1.5 text-right">{pct(d.verified_rate)}</td>
                    <td className="py-1.5 text-right">{pct(d.repaired_rate)}</td>
                    <td className="py-1.5 text-right">{d.judged ? `${pct(d.judge_pass_rate)} (${d.judged})` : "–"}</td>
                    <td className="py-1.5 text-right">{d.answers ? `${secs(d.p50_ms)} / ${secs(d.p95_ms)}` : "–"}</td>
                    <td className="py-1.5 text-right">{usd(d.cost_per_answer_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-5 lg:col-span-2">
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">Golden-set runs (every deploy)</p>
            {q.evals.length === 0 ? <p className="text-xs text-[var(--ink-3)]">No eval runs yet.</p> : (
              <ul className="divide-y divide-[var(--line)] text-xs">
                {q.evals.map((r) => (
                  <li key={r.at} className="py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold">{when(r.at)}</span>
                      <Pill tone={r.passed ? "good" : "bad"}>{r.passed ? "gate passed" : `failed: ${(r.failed ?? []).join(", ")}`}</Pill>
                    </div>
                    <p className="tabular mt-1 text-[var(--ink-3)]">
                      {r.n} questions · grounded {pct(r.grounded_rate)} · judge {pct(r.judge_pass_rate)}{r.judge_trusted === false && " (not gating)"} · p95 {secs(r.p95_ms)} · {usd(r.cost_per_answer_usd)}
                    </p>
                    <p className="mt-0.5 truncate text-[var(--ink-3)]" title={r.prompt_version}>prompt {r.prompt_version}{r.violations?.length ? ` · broke ${r.violations.join(", ")}` : ""}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">Judge calibration (human labels)</p>
            {!cal ? <p className="text-xs text-[var(--ink-3)]">Not calibrated yet: its scores are reported but don't gate.</p> : (
              <div className="space-y-1 text-xs">
                <p className="tabular">κ {cal.kappa?.toFixed(2)} · accuracy {pct(cal.accuracy)} · catches {pct(cal.fail_recall)} of real breaks · {cal.n} labels · {when(cal.at)}</p>
                <p className="text-[var(--ink-3)]">{cal.trusted ? "Agrees with people well enough to gate deploys." : "Below κ 0.6: reported only, not gating."}
                  {cal.unreviewed_labels ? ` ${cal.unreviewed_labels} seed labels still need a human review.` : ""}</p>
                {!!cal.disagreements?.length && <p className="text-[var(--ink-3)]">Disagrees on: {cal.disagreements.join(", ")}</p>}
              </div>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}
