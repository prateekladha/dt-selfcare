import { mbToGb } from "~/lib/format";

/** Meter: one ratio against a limit. Fill carries severity (brand -> warning -> critical) with an
 * icon + text label, never colour alone; the track is a light step of the same hue. */
export function UsageMeter({ usedMb, allowanceMb, pct, policy }: { usedMb: number; allowanceMb: number; pct: number; policy: string }) {
  const level = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "ok";
  const fill = { ok: "var(--brand)", warn: "var(--warn)", bad: "var(--bad)" }[level];
  const track = { ok: "var(--brand-bg)", warn: "var(--warn-bg)", bad: "var(--bad-bg)" }[level];
  const label = level === "bad"
    ? policy === "THROTTLE" ? "⛔ Allowance used, speed reduced" : "⛔ Allowance used, extra data is charged"
    : level === "warn" ? "⚠ Over 80% used" : "✓ On track";
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <p className="text-3xl font-bold text-[var(--ink)]">{mbToGb(usedMb)} <span className="text-base font-medium text-[var(--ink-3)]">of {mbToGb(allowanceMb)} GB</span></p>
        <p className="text-sm font-semibold text-[var(--ink-2)]">{Math.round(pct)}%</p>
      </div>
      <div className="mt-3 h-2.5 w-full overflow-hidden rounded-full" style={{ background: track }}
           role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label="Data used">
        <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(pct, 100)}%`, background: fill }} />
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-[var(--ink-3)]"><span>0</span><span>50%</span><span>80%</span><span>100%</span></div>
      <p className="mt-2 text-sm text-[var(--ink-2)]">{label}</p>
    </div>
  );
}
