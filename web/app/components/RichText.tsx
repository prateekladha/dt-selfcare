import type { ReactNode } from "react";

/** Citation numbering shared by a section: labels (S2, G5, D1…) are shown as 1, 2, 3 in order of first use. */
export class CiteMap {
  private order: string[] = [];
  num(label: string) {
    let i = this.order.indexOf(label);
    if (i < 0) { this.order.push(label); i = this.order.length - 1; }
    return i + 1;
  }
  get labels() { return [...this.order]; }
  /** Assign numbers for every marker in reading order BEFORE rendering (children render later). */
  scan(text: string | null | undefined, extra: string[] = []) {
    for (const m of (text ?? "").matchAll(/\[([DGS]\d{1,2})\]/g)) this.num(m[1]);
    for (const l of extra) this.num(l);
    return this;
  }
}

export function CiteMark({ n, onClick }: { n: number; onClick?: () => void }) {
  return (
    <button type="button" onClick={onClick} aria-label={`Source ${n}`}
            className="relative -top-[0.45em] ml-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--chip)] px-1 text-[10px] font-bold leading-none text-[var(--ink-2)] ring-1 ring-[var(--line-strong)] transition-colors hover:bg-[var(--brand)] hover:text-white hover:ring-[var(--brand)]">
      {n}
    </button>
  );
}

const CITE = /\[([DGS]\d{1,2})\]/g;

/** Inline text: **bold** spans, and [S#]-style markers turned into numbered superscripts.
 *  While an answer streams, a bold span may not be closed yet: an unmatched "**" is hidden, not shown raw. */
export function Inline({ text, cites, onCite }: { text: string; cites: CiteMap; onCite?: (n: number) => void }) {
  const parts = text.split(/\*\*(.+?)\*\*/g);   // odd indexes are the bold spans
  return <>{parts.map((part, i) => {
    const clean = part.replace(/\*\*/g, "");
    if (!clean) return null;
    return i % 2 ? <strong key={i} className="font-semibold"><Cited text={clean} cites={cites} onCite={onCite} /></strong>
                 : <Cited key={i} text={clean} cites={cites} onCite={onCite} />;
  })}</>;
}

function Cited({ text, cites, onCite }: { text: string; cites: CiteMap; onCite?: (n: number) => void }) {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  CITE.lastIndex = 0;
  while ((m = CITE.exec(text))) {
    const before = text.slice(last, m.index).replace(/\s+$/, "");
    if (before) out.push(<span key={`t${last}`}>{before}</span>);
    const n = cites.num(m[1]);
    out.push(<CiteMark key={`c${m.index}`} n={n} onClick={onCite ? () => onCite(n) : undefined} />);
    last = m.index + m[0].length;
  }
  const rest = text.slice(last);
  if (rest) out.push(<span key={`t${last}`}>{rest}</span>);
  return <>{out}</>;
}

/** Paragraphs and "- " bullet lists with consistent spacing; citations rendered as superscripts. */
export function RichText({ text, cites, onCite, className = "" }: { text: string; cites: CiteMap; onCite?: (n: number) => void; className?: string }) {
  // Group consecutive lines into paragraphs, bullet lists ("- ", "* ", "• ") and numbered lists ("1." / "1)").
  // An intro line directly followed by a list (no blank line) is a paragraph and then the list.
  const BULLET = /^[-*•]\s+/, NUMBERED = /^\d{1,2}[.)]\s+/;
  type Block = { kind: "p" | "ul" | "ol"; lines: string[] };
  const blocks: Block[] = [];
  for (const raw of text.trim().split("\n")) {
    const l = raw.trim();
    if (!l) { blocks.push({ kind: "p", lines: [] }); continue; }   // blank line ends the block
    const kind = BULLET.test(l) ? "ul" : NUMBERED.test(l) ? "ol" : "p";
    const last = blocks[blocks.length - 1];
    if (last && last.kind === kind && last.lines.length) last.lines.push(l);
    else blocks.push({ kind, lines: [l] });
  }
  return (
    <div className={`space-y-3 text-[15.5px] leading-[1.7] text-[var(--ink)] ${className}`}>
      {blocks.filter((b) => b.lines.length).map((b, bi) => b.kind === "p"
        ? <p key={bi}><Inline text={b.lines.join(" ")} cites={cites} onCite={onCite} /></p>
        : (() => {
            const List = b.kind === "ul" ? "ul" : "ol";
            return (
              <List key={bi} className={`${b.kind === "ul" ? "list-disc" : "list-decimal"} space-y-1.5 pl-5 marker:text-[var(--ink-3)]`}>
                {b.lines.map((l, li) => <li key={li} className="pl-1"><Inline text={l.replace(b.kind === "ul" ? BULLET : NUMBERED, "")} cites={cites} onCite={onCite} /></li>)}
              </List>
            );
          })())}
    </div>
  );
}

/** Numbered sources list matching the superscripts. */
export function SourceList({ items, highlight }: { items: { n: number; title: string; detail: string; ref?: string }[]; highlight?: number | null }) {
  return (
    <ol className="space-y-2">
      {items.map((s) => (
        <li key={s.n} id={`cite-${s.n}`} className={`flex gap-3 rounded-lg border px-3.5 py-3 transition-colors ${highlight === s.n ? "border-[var(--brand)] bg-[var(--brand-bg)]" : "border-[var(--line)] bg-[var(--surface)]"}`}>
          <span className="mt-0.5 flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--chip)] text-[11px] font-bold ring-1 ring-[var(--line-strong)]">{s.n}</span>
          <div className="min-w-0">
            <p className="text-[13.5px] font-semibold text-[var(--ink)]">{s.title}</p>
            <p className="mt-1 break-words text-[13px] leading-relaxed text-[var(--ink-2)]">{s.detail}</p>
            {s.ref && <p className="mt-1.5 break-all font-mono text-[11px] text-[var(--ink-3)]">{s.ref}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
