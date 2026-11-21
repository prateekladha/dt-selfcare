import { Form, redirect, useNavigation } from "react-router";
import { rawCore } from "~/lib/api.server";
import { sessions } from "~/lib/session.server";
import type { Route } from "./+types/login";

export const meta = () => [{ title: "Sign in · Mobile self-care" }];

const DEMO = [
  ["4915100000002", "Jonas · DE · traveller (roaming spikes)"],
  ["4915100000001", "Lena · DE · streamer (data alerts)"],
  ["4915100000003", "Mia · DE · buys add-ons"],
  ["4915100000004", "Paul · DE · light user"],
  ["48600000001", "Zofia · PL · calls abroad (PLN)"],
  ["48600000002", "Piotr · PL · streamer (PLN)"],
];

export async function action({ request }: Route.ActionArgs) {
  const form = await request.formData();
  let res: Response;
  try {
    res = await rawCore("/v1/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscriber_id: form.get("subscriber_id"), password: form.get("password") }),
    });
  } catch {
    return { error: "We can't reach the service right now. Please try again in a moment." };
  }
  if (!res.ok) return { error: res.status === 429 ? "Too many attempts. Try again later." : "Wrong number or password." };
  const { access_token } = await res.json();
  const session = await sessions.getSession(request.headers.get("Cookie"));
  session.set("token", access_token);
  session.set("sub", String(form.get("subscriber_id")));
  return redirect("/", { headers: { "Set-Cookie": await sessions.commitSession(session) } });
}

export default function Login({ actionData }: Route.ComponentProps) {
  const nav = useNavigation();
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-md rounded-3xl border border-[var(--line)] bg-[var(--card)] p-8 shadow-sm">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--brand)] text-lg font-black text-white">T</div>
          <div><h1 className="text-xl font-bold">Mobile self-care</h1><p className="text-sm text-[var(--ink-3)]">Usage &amp; bill explainer</p></div>
        </div>
        <Form method="post" className="space-y-4">
          <label className="block text-sm font-medium">Mobile number
            <select name="subscriber_id" className="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--card)] px-3 py-2.5">
              {DEMO.map(([id, label]) => <option key={id} value={id}>+{id} · {label}</option>)}
            </select>
          </label>
          <label className="block text-sm font-medium">Password
            <input name="password" type="password" required autoComplete="current-password"
                   className="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--card)] px-3 py-2.5" />
          </label>
          {actionData?.error && <p className="text-sm text-[var(--bad)]">{actionData.error}</p>}
          <button className="w-full rounded-xl bg-[var(--brand)] py-2.5 font-semibold text-white hover:brightness-110 disabled:opacity-60"
                  disabled={nav.state !== "idle"}>{nav.state !== "idle" ? "Signing in…" : "Sign in"}</button>
        </Form>
        <p className="mt-4 text-xs text-[var(--ink-3)]">Demo lines share one password (shared with reviewers separately).</p>
      </div>
    </main>
  );
}
