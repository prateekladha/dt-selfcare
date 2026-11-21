// Formatting is a pure function of (value, locale, currency) with an explicit time zone, so the
// server render and the client hydration produce identical text - no flicker, no mismatch.
export function money(minor: number, currency: string, locale = "en-GB") {
  return new Intl.NumberFormat(locale, { style: "currency", currency }).format(minor / 100);
}

export function cycleLabel(cycle: string, locale = "en-GB", short = false) {
  const [y, m] = cycle.split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { month: short ? "short" : "long", year: short ? "2-digit" : "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, 1)));
}

export function dateLabel(iso: string, locale = "en-GB") {
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
    .format(new Date(iso));
}

export function mbToGb(mb: number) {
  return (mb / 1000).toLocaleString("en-GB", { maximumFractionDigits: 1 });
}
