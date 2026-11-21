import { useState } from "react";

export type DailyUsage = {
  cycle: string; days_in_month: number; through_day: number; current: boolean; data_allowance_mb: number;
  days: { day: number; data_mb: number; roaming_mb: number; voice_min: number; sms: number }[];
  totals: { data_mb: number; roaming_mb: number; voice_min: number; sms: number };
  avg_data_mb_per_day: number; projected_data_mb: number | null; peak_day: number | null;
};

type Metric = "data_mb" | "voice_min" | "sms";
const METRICS: { key: Metric; label: string; unit: (v: number) => string }[] = [
  { key: "data_mb", label: "Data", unit: (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)} GB` : `${v} MB`) },
  { key: "voice_min", label: "Calls", unit: (v) => `${v} min` },
  { key: "sms", label: "SMS", unit: (v) => `${v} SMS` },
];

/** Day-level usage for one month. Single series per view (no legend box; the toggle names it), thin bars
 *  with 4px rounded tops, recessive grid, per-bar hover/focus tooltip. Remaining days of the current
 *  month are shown as empty slots so the pace is visible; for data a dashed line marks the allowance pace. */
export function DailyUsageChart({ usage, monthLabel }: { usage: DailyUsage; monthLabel: string }) {
  const [metric, setMetric] = useState<Metric>("data_mb");
  const m = METRICS.find((x) => x.key === metric)!;
  const total = usage.totals[metric];
  const avg = usage.through_day ? total / usage.through_day : 0;
  const pace = metric === "data_mb" && usage.data_allowance_mb > 0 ? usage.data_allowance_mb / usage.days_in_month : null;
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 px-6">
        <div role="tablist" aria-label="Usage type" className="inline-flex rounded-lg border border-[var(--line-strong)] p-0.5">
          {METRICS.map((x) => (
            <button key={x.key} role="tab" aria-selected={metric === x.key} type="button" onClick={() => setMetric(x.key)}
                    className={`rounded-md px-3 py-1.5 text-[13px] font-semibold ${metric === x.key ? "bg-[var(--ink)] text-[var(--surface)]" : "text-[var(--ink-3)] hover:text-[var(--ink)]"}`}>{x.label}</button>
          ))}
        </div>
        <dl className="flex gap-5 text-[13px]">
          <div><dt className="text-[var(--ink-3)]">Total</dt><dd className="tabular font-bold">{m.unit(total)}</dd></div>
          <div><dt className="text-[var(--ink-3)]">Per day</dt><dd className="tabular font-bold">{m.unit(Math.round(avg))}</dd></div>
          {metric === "data_mb" && usage.projected_data_mb !== null && (
            <div><dt className="text-[var(--ink-3)]">Month-end</dt><dd className={`tabular font-bold ${usage.data_allowance_mb > 0 && usage.projected_data_mb > usage.data_allowance_mb ? "text-[var(--bad)]" : ""}`}>~{m.unit(usage.projected_data_mb)}</dd></div>
          )}
        </dl>
      </div>
      {total === 0 ? (
        <p className="px-6 pb-6 pt-6 text-sm text-[var(--ink-3)]">No {m.label.toLowerCase()} used in {monthLabel} yet.</p>
      ) : (
        <>
          <div className="hidden md:block"><Bars usage={usage} metric={metric} unit={m.unit} pace={pace} W={1120} H={220} /></div>
          <div className="md:hidden"><Bars usage={usage} metric={metric} unit={m.unit} pace={pace} W={430} H={210} /></div>
        </>
      )}
    </div>
  );
}

function Bars({ usage, metric, unit, pace, W, H }: { usage: DailyUsage; metric: Metric; unit: (v: number) => string; pace: number | null; W: number; H: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const padL = 8, padR = 8, padB = 26, padT = 22;
  const n = usage.days_in_month;
  const vals = usage.days.map((d) => d[metric]);
  const max = Math.max(...vals, pace ?? 0, 1) * 1.15;
  const step = (W - padL - padR) / n;
  const barW = Math.max(3, Math.min(22, step - 2));     // >= 2px surface gap between bars
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / max);
  const label = (d: number) => d === 1 || d % 5 === 0 || d === n;
  const day = hover !== null ? usage.days[hover] : null;
  return (
    <div className="relative px-4 pb-2 pt-3">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`Daily ${metric === "data_mb" ? "data" : metric === "voice_min" ? "call minutes" : "SMS"} usage`}>
        {[0.5, 1].map((f) => <line key={f} x1={padL} x2={W - padR} y1={y(max * f / 1.15)} y2={y(max * f / 1.15)} stroke="var(--grid)" />)}
        <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} stroke="var(--line-strong)" />
        {pace !== null && (
          <>
            <line x1={padL} x2={W - padR} y1={y(pace)} y2={y(pace)} stroke="var(--ink-3)" strokeDasharray="2 4" />
            <text x={W - padR - 2} y={y(pace) - 6} textAnchor="end" className="fill-[var(--ink-3)] text-[11px] font-medium">Allowance pace {unit(Math.round(pace))}/day</text>
          </>
        )}
        {Array.from({ length: n }, (_, i) => {
          const d = i + 1;
          const x = padL + i * step + (step - barW) / 2;
          const known = i < usage.days.length;
          const v = known ? vals[i] : 0;
          const base = y(0), top = y(v), r = Math.min(4, barW / 2), h = Math.max(base - top, v > 0 ? 2 : 0);
          const path = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + barW - r} Q${x + barW},${base - h} ${x + barW},${base - h + r} V${base} Z`;
          const dim = hover !== null && hover !== i;
          return (
            <g key={d} tabIndex={known ? 0 : -1} aria-label={known ? `Day ${d}: ${unit(v)}` : `Day ${d}: not yet`}
               onMouseEnter={() => known && setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => known && setHover(i)} onBlur={() => setHover(null)}
               className="outline-none">
              <rect x={padL + i * step} y={0} width={step} height={H} fill="transparent" />
              {known
                ? (h > 0 && <path d={path} fill="var(--brand)" opacity={dim ? 0.35 : 1} style={{ transition: "opacity .15s" }} />)
                : <rect x={x} y={base - 3} width={barW} height={3} rx={1.5} fill="var(--chip)" />}
              {label(d) && <text x={x + barW / 2} y={H - 8} textAnchor="middle" className={`text-[11px] ${hover === i ? "fill-[var(--ink)] font-bold" : "fill-[var(--ink-3)]"}`}>{d}</text>}
            </g>
          );
        })}
      </svg>
      {day && hover !== null && (
        <div className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-lg bg-[var(--ink)] px-3 py-2 text-xs text-[var(--surface)] shadow-[var(--shadow-lg)]"
             style={{ left: `${Math.min(92, Math.max(8, ((padL + hover * step + step / 2) / W) * 100))}%`, top: -6 }}>
          <p className="font-bold">Day {day.day}</p>
          <p className="tabular">{unit(day[metric])}{metric === "data_mb" && day.roaming_mb > 0 ? ` · ${day.roaming_mb} MB roaming outside EU` : ""}</p>
        </div>
      )}
    </div>
  );
}
