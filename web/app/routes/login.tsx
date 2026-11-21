import { useRef, useState } from "react";
import { Form, redirect, useNavigation } from "react-router";
import { AlertCircle, ArrowRight, CreditCard, Eye, EyeOff, Loader2, Lock, Sparkles, Zap } from "lucide-react";
import { Logo } from "~/components/ui";
import { rawCore } from "~/lib/api.server";
import { FLAGS, phoneLabel } from "~/lib/format";
import { sessions } from "~/lib/session.server";
import type { Route } from "./+types/login";

export const meta = () => [{ title: "Sign in · Self-care" }];

const DEMO = [
  { id: "4915100000002", name: "Jonas Weber", country: "DE", story: "Bill jumped in October: roaming outside the EU" },
  { id: "4915100000001", name: "Lena Fischer", country: "DE", story: "Heavy streamer: live 80% / 100% data alerts" },
  { id: "4915100000003", name: "Mia Schulz", country: "DE", story: "Buys data add-ons instead of paying overage" },
  { id: "4915100000004", name: "Paul Becker", country: "DE", story: "Light user: flat bill every month" },
  { id: "48600000001", name: "Zofia Nowak", country: "PL", story: "Calls abroad, billed in PLN with 23% VAT" },
  { id: "48600000002", name: "Piotr Kowalski", country: "PL", story: "Streamer on a PLN plan" },
];

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
  const { access_token } = await res.json();
  const session = await sessions.getSession(request.headers.get("Cookie"));
  session.set("token", access_token);
  session.set("sub", subscriber_id);
  return redirect("/", { headers: { "Set-Cookie": await sessions.commitSession(session) } });
}

export default function Login({ actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  const busy = nav.state !== "idle";
  const [number, setNumber] = useState(actionData?.subscriber_id ? `+${actionData.subscriber_id}` : "");
  const [show, setShow] = useState(false);
  const pw = useRef<HTMLInputElement>(null);

  return (
    <main className="grid min-h-screen lg:grid-cols-[1.05fr_1fr]">
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
        <p className="text-xs text-white/70">Case-study build · demo data only</p>
      </aside>

      <section className="flex items-center justify-center bg-[var(--surface)] px-5 py-12">
        <div className="w-full max-w-[420px]">
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
                <input id="password" ref={pw} name="password" type={show ? "text" : "password"} autoComplete="current-password" required
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
            <p className="flex items-center justify-center gap-1.5 text-xs text-[var(--ink-3)]"><Lock size={12} /> Encrypted connection · session in a secure cookie</p>
          </Form>

          <div className="mt-10">
            <div className="flex items-center gap-3"><span className="h-px flex-1 bg-[var(--line)]" /><span className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-3)]">Demo accounts</span><span className="h-px flex-1 bg-[var(--line)]" /></div>
            <ul className="mt-4 grid gap-2">
              {DEMO.map((d) => (
                <li key={d.id}>
                  <button type="button" onClick={() => { setNumber(phoneLabel(d.id)); pw.current?.focus(); }}
                          className={`flex w-full items-center gap-3 rounded-lg border px-3.5 py-2.5 text-left transition-colors ${number.replace(/\D/g, "") === d.id ? "border-[var(--ink)] bg-[var(--surface-2)]" : "border-[var(--line)] hover:border-[var(--line-strong)] hover:bg-[var(--surface-2)]"}`}>
                    <span className="text-lg" aria-hidden="true">{FLAGS[d.country]}</span>
                    <span className="min-w-0 flex-1"><span className="block text-sm font-semibold">{d.name}</span><span className="block truncate text-[13px] text-[var(--ink-3)]">{d.story}</span></span>
                    <ArrowRight size={16} className="shrink-0 text-[var(--ink-3)]" />
                  </button>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-center text-xs text-[var(--ink-3)]">All demo lines share one password, provided with the case study.</p>
          </div>
        </div>
      </section>
    </main>
  );
}
