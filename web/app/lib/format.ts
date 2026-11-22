// Formatting is a pure function of (value, locale, currency) with an explicit time zone, so the
// server render and the client hydration produce identical text - no flicker, no mismatch.
// The UI copy is English, so numbers and dates are formatted in English too (no "Oktober" next to
// English text); the currency is always the bill's own (EUR / PLN).
const UI_LOCALE = "en-GB";

export function money(minor: number, currency: string, _locale?: string) {
  return new Intl.NumberFormat(UI_LOCALE, { style: "currency", currency }).format(minor / 100);
}

export function cycleLabel(cycle: string, _locale?: string, short = false) {
  const [y, m] = cycle.split("-").map(Number);
  return new Intl.DateTimeFormat(UI_LOCALE, { month: short ? "short" : "long", year: short ? "2-digit" : "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, 1)));
}

export function dateLabel(iso: string, _locale?: string) {
  return new Intl.DateTimeFormat(UI_LOCALE, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
    .format(new Date(iso));
}

export function mbToGb(mb: number) {
  return (mb / 1000).toLocaleString(UI_LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

/** "Roaming data, zone Z2" -> "roaming data in zone Z2" (for use inside a sentence). */
export function inSentence(label: string) {
  return label.charAt(0).toLowerCase() + label.slice(1).replace(", zone ", " in zone ");
}

export function phoneLabel(id: string) {
  // E.164 digits -> "+49 151 0000 0002" style grouping (display only)
  const cc = id.startsWith("49") ? 2 : id.startsWith("48") ? 2 : 2;
  const rest = id.slice(cc);
  return `+${id.slice(0, cc)} ${rest.slice(0, 3)} ${rest.slice(3, 7)} ${rest.slice(7)}`.trim();
}

export const FLAGS: Record<string, string> = { DE: "🇩🇪", PL: "🇵🇱" };

/** Overdue only after the END of the due day: "pay by 15 Nov" is still on time all of 15 Nov
 *  (due dates are stored as 00:00 UTC of that day). Same rule as the PDF, the assistant and order blocking. */
export function isOverdue(dueDate: string, asOf: string | Date = new Date()): boolean {
  return new Date(dueDate).getTime() + 86_400_000 <= new Date(asOf).getTime();
}


/** First sentence of an AI summary for a one-line teaser. Doesn't stop at abbreviations ("incl.", "inkl.", "z.B.",
 * "e.g.", "approx.") or decimals ("41.59"), which used to cut "Your bill is 41.59 EUR incl. VAT ..." at "incl.". */
export function firstSentence(text: string): string {
  const t = text.replace(/\s*\[[DGS]\d{1,2}\]/g, "").trim();
  const abbr = /(?:\b(?:incl|inkl|exkl|excl|approx|ca|bzw|vs|etc|e\.g|i\.e|z\.B|u\.a|d\.h|ok|nr|no|tj|np|zł|ok)\.)$/i;
  const re = /[.!?](?=\s+\p{Lu}|\s*$)/gu;
  let m: RegExpExecArray | null;
  while ((m = re.exec(t))) {
    const head = t.slice(0, m.index + 1);
    if (!abbr.test(head)) return head;
  }
  return t;
}
