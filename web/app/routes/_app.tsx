import { useState } from "react";
import { data, Form, NavLink, Outlet, useNavigation, useRouteLoaderData } from "react-router";
import { Bell, FlaskConical, Home as HomeIcon, LogOut, MessageSquareText, Receipt, Server } from "lucide-react";
import { LiveNotifications } from "~/components/LiveNotifications";
import { Avatar, Logo } from "~/components/ui";
import { api, PRIVATE } from "~/lib/api.server";
import { freshToken } from "~/lib/session.server";
import { dateLabel, FLAGS, phoneLabel } from "~/lib/format";
import { derive } from "~/lib/snapshot.server";
import type { Home } from "~/lib/types";
import type { Route } from "./+types/_app";

export const headers = ({ loaderHeaders }: Route.HeadersArgs) => {
  const h = new Headers(PRIVATE);
  const c = loaderHeaders.get("Set-Cookie");
  if (c) h.set("Set-Cookie", c);
  return h;
};

// One snapshot per navigation for the whole screen (header, page, badges): a single "as of" moment.
// Also keeps the session fresh: the rotated refresh cookie is sent back on every navigation.
export async function loader({ request }: Route.LoaderArgs) {
  const { setCookie } = await freshToken(request);
  const home = await api<Home>(request, "/v1/me/home");
  return data({ home, d: derive(home) }, setCookie ? { headers: { "Set-Cookie": setCookie } } : undefined);
}

export function useSnapshot() {
  return useRouteLoaderData<typeof loader>("routes/_app")!;
}

const NAV = [
  { to: "/", label: "Overview", icon: HomeIcon, end: true },
  { to: "/bills", label: "Bills", icon: Receipt },
  { to: "/insights", label: "Try your usage", icon: FlaskConical },
  { to: "/assistant", label: "Assistant", icon: MessageSquareText },
];

export default function AppLayout({ loaderData }: Route.ComponentProps) {
  const { home, d } = loaderData;
  const nav = useNavigation();
  const [menu, setMenu] = useState<"none" | "bell" | "account">("none");
  const toggle = (m: "bell" | "account") => setMenu((cur) => (cur === m ? "none" : m));

  return (
    <div className="min-h-screen pb-20 md:pb-0">
      {nav.state !== "idle" && (
        <div className="fixed inset-x-0 top-0 z-[60] h-0.5 overflow-hidden bg-[var(--brand-bg)]">
          <div className="h-full w-1/3 bg-[var(--brand)]" style={{ animation: "progress 1s ease-in-out infinite" }} />
        </div>
      )}
      <header className="sticky top-0 z-50 border-b border-[var(--line)] bg-[var(--surface)]">
        <div className="mx-auto flex h-16 max-w-[1200px] items-center gap-8 px-5">
          <NavLink to="/" className="shrink-0"><Logo /></NavLink>
          <nav className="hidden h-full items-stretch gap-1 md:flex" aria-label="Main">
            {NAV.map(({ to, label, end }) => (
              <NavLink key={to} to={to} end={end} prefetch="intent"
                className={({ isActive }) => `relative flex items-center px-3 text-[15px] font-semibold transition-colors ${isActive ? "text-[var(--ink)]" : "text-[var(--ink-3)] hover:text-[var(--ink)]"}`}>
                {({ isActive }) => (<>{label}{isActive && <span className="absolute inset-x-3 bottom-0 h-[3px] rounded-t bg-[var(--brand)]" />}</>)}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-1.5">
            <div className="relative">
              <button onClick={() => toggle("bell")} aria-label={`Notifications${d.unread ? `, ${d.unread} unread` : ""}`}
                      className="relative flex h-10 w-10 items-center justify-center rounded-full text-[var(--ink-2)] hover:bg-[var(--chip)]">
                <Bell size={20} />
                {d.unread > 0 && <span className="absolute right-1.5 top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--brand)] px-1 text-[10px] font-bold text-white">{d.unread}</span>}
              </button>
              {menu === "bell" && (
                <div className="fixed inset-x-3 top-[68px] z-50 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--surface)] shadow-[var(--shadow-lg)] md:absolute md:inset-x-auto md:right-0 md:top-12 md:w-[380px]">
                  <p className="border-b border-[var(--line)] px-4 py-3 text-sm font-bold">Notifications</p>
                  {home.notifications.length === 0 ? <p className="px-4 py-6 text-sm text-[var(--ink-3)]">You're all caught up.</p> : (
                    <ul className="max-h-[380px] divide-y divide-[var(--line)] overflow-auto">
                      {home.notifications.map((n) => (
                        <li key={n.id} className="flex gap-3 px-4 py-3">
                          <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${n.read ? "bg-transparent" : "bg-[var(--brand)]"}`} />
                          <div><p className="text-sm font-semibold">{n.title}</p><p className="mt-0.5 text-[13px] text-[var(--ink-3)]">{n.body}</p>
                            <p className="mt-1 text-xs text-[var(--ink-3)]">{dateLabel(n.created_at, home.subscriber.locale)}</p></div>
                        </li>))}
                    </ul>)}
                </div>
              )}
            </div>
            <div className="relative">
              <button onClick={() => toggle("account")} className="flex items-center gap-2 rounded-full p-1 pr-2 hover:bg-[var(--chip)]" aria-label="Account">
                <Avatar name={home.subscriber.name} size={32} />
                <span className="hidden text-sm font-semibold lg:inline">{home.subscriber.name.split(" ")[0]}</span>
              </button>
              {menu === "account" && (
                <div className="fixed inset-x-3 top-[68px] z-50 rounded-xl border md:absolute md:inset-x-auto md:right-0 md:top-12 md:w-72 border-[var(--line)] bg-[var(--surface)] p-2 shadow-[var(--shadow-lg)]">
                  <div className="flex items-center gap-3 px-3 py-3">
                    <Avatar name={home.subscriber.name} size={40} />
                    <div className="min-w-0"><p className="truncate font-bold">{home.subscriber.name}</p>
                      <p className="text-[13px] text-[var(--ink-3)]">{phoneLabel(home.subscriber.id)} {FLAGS[home.subscriber.country]}</p>
                      <p className="text-[13px] text-[var(--ink-3)]">{home.plan.name}</p></div>
                  </div>
                  <NavLink to="/system" onClick={() => setMenu("none")}
                           className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--chip)]"><Server size={16} /> System status</NavLink>
                  <Form method="post" action="/logout">
                    <button className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-sm font-semibold text-[var(--ink-2)] hover:bg-[var(--chip)]"><LogOut size={16} /> Sign out</button>
                  </Form>
                </div>
              )}
            </div>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1200px] px-5 py-8" onClick={() => menu !== "none" && setMenu("none")}><Outlet /></main>
      <nav className="fixed inset-x-0 bottom-0 z-50 grid grid-cols-4 border-t border-[var(--line)] bg-[var(--surface)] md:hidden" aria-label="Main">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => `flex flex-col items-center gap-1 py-2.5 text-[11px] font-semibold ${isActive ? "text-[var(--brand)]" : "text-[var(--ink-3)]"}`}>
            <Icon size={20} />{label.replace("Try your usage", "Try usage")}
          </NavLink>
        ))}
      </nav>
      <LiveNotifications />
    </div>
  );
}
