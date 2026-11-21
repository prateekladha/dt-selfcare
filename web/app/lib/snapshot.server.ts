import type { Home } from "./types";

/** Derived, display-ready facts computed ONCE on the server from the snapshot's as_of time, in the
 * customer's own time zone - so server render and client hydration can never disagree. */
export function derive(home: Home) {
  const tz = home.subscriber.country === "PL" ? "Europe/Warsaw" : "Europe/Berlin";
  const asOf = new Date(home.as_of);
  const [y, m] = home.usage.cycle.split("-").map(Number);
  const daysInCycle = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const day = Math.min(daysInCycle, Math.max(1, asOf.getUTCDate()));
  const daysLeft = daysInCycle - day;
  const projectedMb = Math.round((home.usage.data_mb / day) * daysInCycle);
  const hour = Number(new Intl.DateTimeFormat("en-GB", { hour: "numeric", hourCycle: "h23", timeZone: tz }).format(asOf));
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const asOfLabel = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz }).format(asOf);
  const due = home.trend.find((b) => b.id === home.outstanding.bill_id) ?? null;
  const overdue = !!due && new Date(due.due_date) < asOf;
  const unread = home.notifications.filter((n) => !n.read).length;
  return { tz, day, daysInCycle, daysLeft, projectedMb, greeting, asOfLabel, due, overdue, unread };
}
export type Derived = ReturnType<typeof derive>;
