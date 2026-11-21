import { Download, Receipt } from "lucide-react";
import { Link } from "react-router";
import { Card, Pill } from "~/components/ui";
import { cycleLabel, dateLabel, money } from "~/lib/format";
import { useSnapshot } from "./_app";

export const meta = () => [{ title: "Bills · Self-care" }];

export default function Bills() {
  const { home, d } = useSnapshot();
  const loc = home.subscriber.locale;
  const bills = [...home.trend].reverse();
  return (
    <div className="space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold">Bills</h1>
        <p className="mt-1 text-[15px] text-[var(--ink-3)]">Your last {bills.length} bills. Open one to see the breakdown and why it changed.</p>
      </div>
      {/* phones: stacked list */}
      <Card className="overflow-hidden sm:hidden">
        <ul className="divide-y divide-[var(--line)]">
          {bills.map((b) => {
            const overdue = b.outstanding > 0 && new Date(b.due_date) < new Date(home.as_of);
            return (
              <li key={b.cycle} className="flex items-center gap-3 px-4 py-3.5">
                <Link to={`/bills/${b.cycle}`} className="min-w-0 flex-1">
                  <p className="font-semibold">{cycleLabel(b.cycle, loc)}</p>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <span className="tabular font-bold">{money(b.total, b.currency, loc)}</span>
                    {b.outstanding > 0 ? <Pill tone={overdue ? "bad" : "warn"}>{overdue ? "Overdue" : `Due ${dateLabel(b.due_date, loc)}`}</Pill> : <Pill tone="good">Paid</Pill>}
                  </div>
                </Link>
                <a href={`/bills/${b.cycle}/pdf`} download aria-label={`Download ${cycleLabel(b.cycle, loc)} PDF`} className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-[var(--ink-3)] hover:bg-[var(--chip)]"><Download size={18} /></a>
              </li>
            );
          })}
        </ul>
      </Card>
      {/* wider screens: table */}
      <Card className="hidden overflow-hidden sm:block">
        <table className="w-full text-left">
          <thead className="border-b border-[var(--line)] bg-[var(--surface-2)] text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">
            <tr><th className="px-6 py-3">Month</th><th className="hidden px-6 py-3 sm:table-cell">Issued</th><th className="px-6 py-3 text-right">Amount</th><th className="px-6 py-3">Status</th><th className="w-12 px-4 py-3"><span className="sr-only">Download</span></th></tr>
          </thead>
          <tbody className="divide-y divide-[var(--line)]">
            {bills.map((b) => {
              const overdue = b.outstanding > 0 && new Date(b.due_date) < new Date(home.as_of);
              return (
                <tr key={b.cycle} className="group transition-colors hover:bg-[var(--surface-2)]">
                  <td className="px-6 py-4"><Link to={`/bills/${b.cycle}`} className="flex items-center gap-3 font-semibold after:absolute"><span className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--chip)] text-[var(--ink-2)]"><Receipt size={16} /></span>{cycleLabel(b.cycle, loc)}</Link></td>
                  <td className="hidden px-6 py-4 text-sm text-[var(--ink-3)] sm:table-cell">{dateLabel(b.issued_at, loc)}</td>
                  <td className="tabular px-6 py-4 text-right font-bold">{money(b.total, b.currency, loc)}</td>
                  <td className="px-6 py-4">{b.outstanding > 0 ? <Pill tone={overdue ? "bad" : "warn"}>{overdue ? "Overdue" : `Due ${dateLabel(b.due_date, loc)}`}</Pill> : <Pill tone="good">Paid</Pill>}</td>
                  <td className="px-4 py-4"><a href={`/bills/${b.cycle}/pdf`} download aria-label={`Download ${cycleLabel(b.cycle, loc)} PDF`} className="flex h-9 w-9 items-center justify-center rounded-lg text-[var(--ink-3)] hover:bg-[var(--chip)] hover:text-[var(--ink)]"><Download size={17} /></a></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      {d.due && <p className="text-sm text-[var(--ink-3)]">Outstanding: <b className="text-[var(--ink)]">{money(home.outstanding.amount, home.outstanding.currency, loc)}</b>. <Link to="/" className="font-semibold text-[var(--ink)] underline">Pay from the overview</Link></p>}
    </div>
  );
}
