import { Form, NavLink, Outlet } from "react-router";
import { LiveNotifications } from "~/components/LiveNotifications";

export default function AppLayout() {
  const link = ({ isActive }: { isActive: boolean }) =>
    `rounded-lg px-3 py-1.5 text-sm font-medium ${isActive ? "bg-[var(--brand-bg)] text-[var(--brand-ink)]" : "text-[var(--ink-2)] hover:bg-[var(--chip)]"}`;
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-[var(--line)] bg-[var(--card)]/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--brand)] font-black text-white">T</div>
            <span className="hidden font-bold sm:inline">Mobile self-care</span>
          </div>
          <nav className="flex gap-1">
            <NavLink to="/" end className={link}>Overview</NavLink>
            <NavLink to="/insights" className={link}>Try your usage</NavLink>
          </nav>
          <Form method="post" action="/logout"><button className="text-sm text-[var(--ink-3)] hover:text-[var(--ink)]">Sign out</button></Form>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6"><Outlet /></main>
      <LiveNotifications />
    </div>
  );
}
