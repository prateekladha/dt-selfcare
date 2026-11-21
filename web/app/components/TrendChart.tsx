import { useState } from "react";
import { Link } from "react-router";
import type { BillSummary } from "~/lib/types";
import { cycleLabel, money } from "~/lib/format";

/** 12-month bill trend: single series (no legend), thin rounded bars anchored to the baseline,
 * per-bar hover tooltip, click-through to the bill. Unpaid bills get an outlined bar + label. */
export function TrendChart({ bills, locale }: { bills: BillSummary[]; locale: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!bills.length) return <p className="text-sm text-[var(--ink-3)]">No bills yet.</p>;
  const W = 640, H = 220, padL = 44, padB = 28, padT = 16;
  const max = Math.max(...bills.map((b) => b.total)) * 1.15;
  const step = (W - padL) / bills.length;
  const barW = Math.min(24, step - 8);
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / max);
  const ticks = [0, max / 2, max].map((t) => Math.round(t / 100) * 100);
  const cur = bills[0].currency;
  const avg = bills.reduce((s, b) => s + b.total, 0) / bills.length;

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`Bill totals for the last ${bills.length} months`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={W} y1={y(t)} y2={y(t)} stroke="var(--grid)" strokeWidth={1} />
            <text x={padL - 8} y={y(t) + 4} textAnchor="end" className="fill-[var(--ink-3)] text-[11px]">
              {money(t, cur, locale).replace(/[.,]00(?=\D*$)/, "")}
            </text>
          </g>
        ))}
        <line x1={padL} x2={W} y1={y(avg)} y2={y(avg)} stroke="var(--ink-3)" strokeDasharray="3 4" strokeWidth={1} />
        {bills.map((b, i) => {
          const x = padL + i * step + (step - barW) / 2;
          const top = y(b.total), base = y(0), r = 4;
          const h = Math.max(base - top, 2);
          const unpaid = b.outstanding > 0;
          const path = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + barW - r} Q${x + barW},${base - h} ${x + barW},${base - h + r} V${base} Z`;
          return (
            <Link key={b.cycle} to={`/bills/${b.cycle}`} aria-label={`${cycleLabel(b.cycle, locale)}: ${money(b.total, cur, locale)}`}>
              <rect x={padL + i * step} y={padT} width={step} height={H - padT} fill="transparent"
                    onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(i)} />
              <path d={path} fill={unpaid ? "var(--card)" : "var(--brand)"} stroke="var(--brand)" strokeWidth={unpaid ? 2 : 0}
                    opacity={hover === null || hover === i ? 1 : 0.55} pointerEvents="none" />
              <text x={x + barW / 2} y={H - 8} textAnchor="middle" className="fill-[var(--ink-3)] text-[11px]">
                {cycleLabel(b.cycle, locale, true)}
              </text>
            </Link>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="pointer-events-none absolute -translate-x-1/2 rounded-lg border border-[var(--line)] bg-[var(--card)] px-3 py-2 text-xs shadow-lg"
             style={{ left: `${((padL + hover * step + step / 2) / W) * 100}%`, top: 0 }}>
          <div className="font-semibold text-[var(--ink)]">{cycleLabel(bills[hover].cycle, locale)}</div>
          <div className="text-[var(--ink-2)]">{money(bills[hover].total, cur, locale)} incl. VAT</div>
          <div className="text-[var(--ink-3)]">{bills[hover].outstanding > 0 ? `Due ${money(bills[hover].outstanding, cur, locale)}` : "Paid"}</div>
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-4 text-xs text-[var(--ink-3)]">
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[var(--brand)]" />Paid</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm border-2 border-[var(--brand)]" />Unpaid</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-4 border-t border-dashed border-[var(--ink-3)]" />12-month average {money(Math.round(avg), cur, locale)}</span>
      </div>
    </div>
  );
}
