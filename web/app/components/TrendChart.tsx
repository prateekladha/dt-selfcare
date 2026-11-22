import { useState } from "react";
import { useNavigate } from "react-router";
import type { BillSummary } from "~/lib/types";
import { cycleLabel, money } from "~/lib/format";

/** 12-month bill trend. Single series (no legend box), thin bars with 4px rounded tops anchored to
 * the baseline, recessive grid, per-bar hover/focus tooltip, click-through. Unpaid = outlined. */
export function TrendChart({ bills, locale, selected }: { bills: BillSummary[]; locale: string; selected?: string }) {
  if (!bills.length) return <p className="px-6 pb-6 text-sm text-[var(--ink-3)]">No bills yet.</p>;
  // Phones get the last 6 months at a readable scale; wider screens the full 12.
  return (
    <>
      <div className="hidden md:block"><Chart bills={bills} locale={locale} selected={selected} W={1120} H={260} /></div>
      <div className="md:hidden"><Chart bills={bills.slice(-6)} locale={locale} selected={selected} W={430} H={250} /></div>
    </>
  );
}

function Chart({ bills, locale, selected, W, H }: { bills: BillSummary[]; locale: string; selected?: string; W: number; H: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const navigate = useNavigate();
  const padL = 8, padR = 8, padB = 30, padT = 28;
  const max = Math.max(...bills.map((b) => b.total)) * 1.18;
  const step = (W - padL - padR) / bills.length;
  const barW = Math.min(34, step * 0.42);
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / max);
  const cur = bills[0].currency;
  const avg = bills.reduce((s, b) => s + b.total, 0) / bills.length;
  const active = hover ?? -1;  // dim others only while hovering; the latest bill is marked by its label
  const latest = bills.findIndex((b) => b.cycle === selected);

  return (
    <div className="relative px-4 pb-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`Bill totals, last ${bills.length} months`}>
        {[0.25, 0.5, 0.75, 1].map((f) => <line key={f} x1={padL} x2={W - padR} y1={y(max * f / 1.18)} y2={y(max * f / 1.18)} stroke="var(--grid)" />)}
        <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} stroke="var(--line-strong)" />
        <line x1={padL} x2={W - padR} y1={y(avg)} y2={y(avg)} stroke="var(--ink-3)" strokeDasharray="2 4" />
        <text x={padL + 2} y={y(avg) - 6} className="fill-[var(--ink-3)] text-[11px] font-medium">{bills.length}-month average {money(Math.round(avg), cur, locale)}</text>
        {bills.map((b, i) => {
          const x = padL + i * step + (step - barW) / 2;
          const top = y(b.total), base = y(0), r = 4, h = Math.max(base - top, 3);
          const unpaid = b.outstanding > 0;
          const d = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + barW - r} Q${x + barW},${base - h} ${x + barW},${base - h + r} V${base} Z`;
          const dim = active >= 0 && active !== i;
          return (
            <g key={b.cycle} tabIndex={0} role="link" aria-label={`${cycleLabel(b.cycle, locale)} ${money(b.total, cur, locale)}${unpaid ? ", unpaid" : ""}`}
               onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}
               onClick={() => navigate(`/bills/${b.cycle}`)} onKeyDown={(e) => e.key === "Enter" && navigate(`/bills/${b.cycle}`)} className="cursor-pointer outline-none">
              <rect x={padL + i * step} y={0} width={step} height={H} fill="transparent" />
              <path d={d} fill={unpaid ? "var(--surface)" : "var(--brand)"} stroke="var(--brand)" strokeWidth={unpaid ? 2 : 0} opacity={dim ? 0.35 : 1} style={{ transition: "opacity .15s" }} />
              <text x={x + barW / 2} y={H - 10} textAnchor="middle" className={`text-[12px] ${active === i || (active < 0 && latest === i) ? "fill-[var(--ink)] font-bold" : "fill-[var(--ink-3)]"}`}>{cycleLabel(b.cycle, locale, true)}</text>
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-lg bg-[var(--ink)] px-3 py-2 text-xs text-[var(--surface)] shadow-[var(--shadow-lg)]"
             style={{ left: `${((padL + hover * step + step / 2) / W) * 100}%`, top: -8 }}>
          <p className="font-bold">{cycleLabel(bills[hover].cycle, locale)}</p>
          <p className="tabular">{money(bills[hover].total, cur, locale)} · {bills[hover].outstanding > 0 ? "unpaid" : "paid"}</p>
        </div>
      )}
    </div>
  );
}
