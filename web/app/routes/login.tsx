import { useState } from "react";
import { Form, redirect, useNavigation } from "react-router";
import { AlertCircle, ArrowRight, CreditCard, Eye, EyeOff, Loader2, Sparkles, Zap } from "lucide-react";
import { Logo } from "~/components/ui";
import { rawCore } from "~/lib/api.server";
import { sessions } from "~/lib/session.server";
import type { Route } from "./+types/login";

export const meta = () => [{ title: "Sign in · Self-care" }];


export async function loader({ request }: Route.LoaderArgs) {
  const session = await sessions.getSession(request.headers.get("Cookie"));
  if (session.get("token")) throw redirect("/");
  return null;
}

export async function action({ request }: Route.ActionArgs) {
  const form = await request.formData();
  const subscriber_id = String(form.get("subscriber_id") ?? "").replace(/[^\d]/g, "");
  let res: Response;
  try {
    res = await rawCore("/v1/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscriber_id, password: form.get("password") }),
    });
  } catch {
    return { error: "We can't reach the service right now. Please try again in a moment.", subscriber_id };
  }
  if (!res.ok) {
    return { error: res.status === 429 ? "Too many attempts. Please wait a few minutes and try again." : "That number and password don't match.", subscriber_id };
  }
  const { access_token, refresh_token } = await res.json();
  const session = await sessions.getSession(request.headers.get("Cookie"));
  session.set("token", access_token);
  session.set("refresh", refresh_token);
  session.set("sub", subscriber_id);
  return redirect("/", { headers: { "Set-Cookie": await sessions.commitSession(session) } });
}

export default function Login({ actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  const busy = nav.state !== "idle";
  const [number, setNumber] = useState(actionData?.subscriber_id ? `+${actionData.subscriber_id}` : "");
  const [show, setShow] = useState(false);

  return (
    <main className="grid min-h-screen overflow-x-hidden lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      <aside className="relative hidden overflow-hidden bg-[var(--brand)] p-12 text-white lg:flex lg:flex-col">
        <Logo inverted />
        <div className="my-auto max-w-[480px]">
          <h1 className="display text-[52px] font-extrabold leading-[1.02]">Your bill,<br />finally explained.</h1>
          <p className="mt-5 text-lg text-white/85">See what you've used in real time, why every bill changed, and pay in seconds.</p>
          <ul className="mt-10 space-y-5">
            {[[Zap, "Live usage and unbilled amount, updated as you use your phone"],
              [Sparkles, "Every change explained in plain words, in your language"],
              [CreditCard, "Pay with UPI or card. Safe, and never charged twice"]].map(([Icon, t]) => {
              const I = Icon as typeof Zap;
              return <li key={t as string} className="flex items-start gap-4"><span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-white/15"><I size={19} /></span><span className="pt-2 text-[15px] text-white/95">{t as string}</span></li>;
            })}
          </ul>
        </div>
        <svg className="pointer-events-none absolute -bottom-10 -right-10 opacity-[0.13]" width="420" height="300" viewBox="0 0 420 300" aria-hidden="true">
          {[60, 110, 80, 150, 120, 190, 240].map((h, i) => <rect key={i} x={i * 60} y={300 - h} width="36" height={h} rx="6" fill="#fff" />)}
        </svg>
      </aside>

      <section className="flex min-w-0 items-center justify-center bg-[var(--surface)] px-5 py-12">
        <div className="w-full min-w-0 max-w-[420px]">
          <div className="mb-10 lg:hidden"><Logo /></div>
          <h2 className="display text-[32px] font-extrabold">Sign in</h2>
          <p className="mt-1.5 text-[15px] text-[var(--ink-3)]">Use your mobile number and password.</p>

          <Form method="post" className="mt-8 space-y-5" noValidate>
            <div>
              <label htmlFor="subscriber_id" className="mb-1.5 block text-sm font-semibold">Mobile number</label>
              <input id="subscriber_id" name="subscriber_id" inputMode="tel" autoComplete="username" required placeholder="+49 151 0000 0002"
                     value={number} onChange={(e) => setNumber(e.target.value)}
                     className="h-12 w-full rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] px-4 text-[16px] outline-none transition focus:border-[var(--ink)] focus:ring-4 focus:ring-[var(--chip)]" />
            </div>
            <div>
              <label htmlFor="password" className="mb-1.5 block text-sm font-semibold">Password</label>
              <div className="relative">
                <input id="password" name="password" type={show ? "text" : "password"} autoComplete="current-password" required
                       className="h-12 w-full rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] px-4 pr-12 text-[16px] outline-none transition focus:border-[var(--ink)] focus:ring-4 focus:ring-[var(--chip)]" />
                <button type="button" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide password" : "Show password"}
                        className="absolute right-1.5 top-1.5 flex h-9 w-9 items-center justify-center rounded-md text-[var(--ink-3)] hover:bg-[var(--chip)]">
                  {show ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </div>
            {actionData?.error && (
              <div role="alert" className="flex items-start gap-2.5 rounded-lg bg-[var(--bad-bg)] px-4 py-3 text-sm font-medium text-[var(--bad)]">
                <AlertCircle size={18} className="mt-px shrink-0" />{actionData.error}
              </div>
            )}
            <button disabled={busy} className="flex h-12 w-full items-center justify-center gap-2 rounded-lg bg-[var(--brand)] text-[16px] font-bold text-white transition-colors hover:bg-[var(--brand-hover)] disabled:opacity-70">
              {busy ? <><Loader2 size={18} className="animate-spin" /> Signing in…</> : <>Sign in <ArrowRight size={18} /></>}
            </button>
          </Form>


        </div>
      </section>
    </main>
  );
}
