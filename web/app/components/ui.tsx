import type { ReactNode } from "react";
import { Link } from "react-router";

export function Card({ children, className = "", as: As = "section" }: { children: ReactNode; className?: string; as?: "section" | "div" }) {
  return <As className={`rounded-[var(--radius)] border border-[var(--line)] bg-[var(--surface)] shadow-[var(--shadow)] ${className}`}>{children}</As>;
}

export function CardHeader({ title, subtitle, action, icon }: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 px-6 pt-5">
      <div className="flex items-start gap-3">
        {icon && <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[var(--chip)] text-[var(--ink-2)]">{icon}</div>}
        <div>
          <h2 className="text-[15px] font-semibold text-[var(--ink)]">{title}</h2>
          {subtitle && <p className="mt-0.5 text-[13px] text-[var(--ink-3)]">{subtitle}</p>}
        </div>
      </div>
      {action}
    </div>
  );
}

type Tone = "neutral" | "good" | "warn" | "bad" | "brand";
const TONES: Record<Tone, string> = {
  neutral: "bg-[var(--chip)] text-[var(--ink-2)]",
  good: "bg-[var(--good-bg)] text-[var(--good)]",
  warn: "bg-[var(--warn-bg)] text-[var(--warn)]",
  bad: "bg-[var(--bad-bg)] text-[var(--bad)]",
  brand: "bg-[var(--brand-bg)] text-[var(--brand-ink)]",
};

export function Pill({ tone = "neutral", children, icon }: { tone?: Tone; children: ReactNode; icon?: ReactNode }) {
  return <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-semibold ${TONES[tone]}`}>{icon}{children}</span>;
}

const BTN = {
  primary: "bg-[var(--brand)] text-white shadow-sm hover:bg-[var(--brand-hover)] disabled:opacity-60",
  secondary: "border border-[var(--line-strong)] bg-[var(--surface)] text-[var(--ink)] hover:bg-[var(--chip)]",
  ghost: "text-[var(--ink-2)] hover:bg-[var(--chip)]",
};
const SIZE = { md: "h-11 px-5 text-[15px]", sm: "h-9 px-3.5 text-sm" };  // 44px touch target

export function Button({ variant = "primary", size = "md", className = "", ...rest }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof BTN; size?: keyof typeof SIZE }) {
  return <button {...rest} className={`inline-flex items-center justify-center gap-2 rounded-lg font-bold transition-colors disabled:cursor-not-allowed ${BTN[variant]} ${SIZE[size]} ${className}`} />;
}

export function LinkButton({ to, variant = "secondary", size = "md", children, className = "", ...rest }:
  { to: string; variant?: keyof typeof BTN; size?: keyof typeof SIZE; children: ReactNode; className?: string } & Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  const cls = `inline-flex items-center justify-center gap-2 rounded-lg font-bold transition-colors ${BTN[variant]} ${SIZE[size]} ${className}`;
  if (rest.download !== undefined || to.endsWith("/pdf")) return <a href={to} className={cls} {...rest}>{children}</a>;
  return <Link to={to} className={cls} {...(rest as object)}>{children}</Link>;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-[var(--chip)] ${className}`} />;
}

export function Avatar({ name, size = 36 }: { name: string; size?: number }) {
  const initials = name.split(" ").map((p) => p[0]).slice(0, 2).join("");
  return (
    <span className="inline-flex shrink-0 items-center justify-center rounded-full bg-[var(--brand-bg)] font-semibold text-[var(--brand-ink)]"
          style={{ width: size, height: size, fontSize: size * 0.38 }}>{initials}</span>
  );
}

/** Neutral product mark (deliberately NOT an operator logo): three rising signal bars. */
export function Logo({ inverted = false }: { inverted?: boolean }) {
  const c = inverted ? "#fff" : "var(--brand)";
  return (
    <span className="inline-flex items-center gap-2.5" aria-label="Self-care">
      <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
        <rect x="3" y="15" width="5" height="10" rx="1.5" fill={c} opacity="0.55" />
        <rect x="11.5" y="9" width="5" height="16" rx="1.5" fill={c} opacity="0.8" />
        <rect x="20" y="3" width="5" height="22" rx="1.5" fill={c} />
      </svg>
      <span className={`display text-[18px] font-extrabold ${inverted ? "text-white" : "text-[var(--ink)]"}`}>Self-care</span>
    </span>
  );
}
