import type { ReactNode } from "react";

export function Card({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl border border-[var(--line)] bg-[var(--card)] p-5 shadow-sm ${className}`}>
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-semibold tracking-wide text-[var(--ink-2)] uppercase">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Pill({ tone = "neutral", children }: { tone?: "neutral" | "good" | "warn" | "bad" | "brand"; children: ReactNode }) {
  const tones = {
    neutral: "bg-[var(--chip)] text-[var(--ink-2)]",
    good: "bg-[var(--good-bg)] text-[var(--good)]",
    warn: "bg-[var(--warn-bg)] text-[var(--warn)]",
    bad: "bg-[var(--bad-bg)] text-[var(--bad)]",
    brand: "bg-[var(--brand-bg)] text-[var(--brand-ink)]",
  };
  return <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

export function Button({ children, variant = "primary", ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" }) {
  const v = variant === "primary"
    ? "bg-[var(--brand)] text-white hover:brightness-110 disabled:opacity-50"
    : "border border-[var(--line)] text-[var(--ink)] hover:bg-[var(--chip)]";
  return <button {...rest} className={`inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition ${v} ${rest.className ?? ""}`}>{children}</button>;
}
