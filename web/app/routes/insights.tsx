import { useEffect, useId, useRef, useState } from "react";
import { Form, useNavigation } from "react-router";
import { Calculator, Check, ChevronDown, FileUp, Globe, Loader2, Phone, Plane, Sun, Tv, Upload } from "lucide-react";
import { DeltaPill, ExplanationSection } from "~/components/Explain";
import { Card, CardHeader } from "~/components/ui";
import { api, ApiError, PRIVATE } from "~/lib/api.server";
import { cycleLabel, money } from "~/lib/format";
import type { Explanation, Line, LlmExplanation, SourceDoc } from "~/lib/types";
import { useSnapshot } from "./_app";
import type { Route } from "./+types/insights";

export const headers = () => PRIVATE;
export const meta = () => [{ title: "Try your usage · Self-care" }];

type SimResult = { source: string; scenario_title?: string | null; compared_to: string | null; baseline: string | null; llm: LlmExplanation | null; status: string; sources: SourceDoc[];
  bill: { lines: Line[]; subtotal: number; vat: number; total: number; currency: string }; explanation: Explanation };

const ICONS: Record<string, typeof Plane> = { "us-trip-2-weeks": Plane, "eu-summer-holiday": Sun, "heavy-streaming": Tv,
  "calling-family-abroad": Phone, "light-month": Globe };
const BLURB: Record<string, string> = { "us-trip-2-weeks": "+1.5 GB data and 90 min calls in zone 2", "eu-summer-holiday": "+6 GB and 150 min in the EU, roam like at home",
  "heavy-streaming": "45 GB of data instead of your usual", "calling-family-abroad": "+400 min international calls", "light-month": "1 GB and an hour of calls instead of your usual" };

export async function loader({ request }: Route.LoaderArgs) {
  return { scenarios: await api<{ key: string; title: string }[]>(request, "/v1/me/usage/scenarios") };
}

/** "Build your own": the fields the API takes (it validates and caps them; these are only for the form). */
const CUSTOM = ["start", "data_gb", "voice_min", "intl_min", "sms", "trip_zone", "trip_data_gb", "trip_voice_min"] as const;

export async function action({ request }: Route.ActionArgs) {
  const form = await request.formData();
  const body = new FormData();
  const file = form.get("file");
  if (form.get("custom")) {
    body.set("custom", "1");
    for (const k of CUSTOM) { const v = String(form.get(k) ?? "").trim().replace(",", "."); if (v) body.set(k, v); }
  } else {
    if (file instanceof File && file.size > 1_000_000) return { error: "That file is larger than 1 MB. Please upload a smaller usage file." };
    if (file instanceof File && file.size > 0) body.set("file", file, file.name);
    else if (form.get("scenario")) body.set("scenario", String(form.get("scenario")));
    else return { error: "Choose a scenario or upload a file." };
  }
  try {
    return { result: await api<SimResult>(request, "/v1/me/usage/simulate", { method: "POST", body }), picked: form.get("custom") ? "custom" : String(form.get("scenario") ?? "") };
  } catch (e) {
    if (e instanceof ApiError) return { error: e.status >= 500 ? "We couldn't price that right now. Please try again in a moment." : e.message.replace(/^invalid usage: /, "") };
    throw e;
  }
}

export default function Insights({ loaderData, actionData }: Route.ComponentProps) {
  const { scenarios } = loaderData;
  const { home } = useSnapshot();
  const loc = home.subscriber.locale;
  const nav = useNavigation();
  const busy = nav.state === "submitting";
  const pending = busy ? (nav.formData?.get("custom") ? "custom" : String(nav.formData?.get("scenario") ?? "file")) : "";
  const [start, setStart] = useState<"usual" | "scratch">("usual");
  const r = actionData && "result" in actionData ? actionData.result : null;
  const [fileName, setFileName] = useState("");
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="display text-[34px] font-extrabold">Try your usage</h1>
        <p className="mt-1 max-w-[720px] text-[15px] text-[var(--ink-3)]">Scenarios start from your real usage last month (a trip adds to it, a lighter or heavier month replaces it) (from your network usage records) and priced on your plan ({home.plan.name}). Or upload a usage file. Your real bill is never affected.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader title="Pick a scenario" />
          <Form method="post" className="grid gap-3 p-6 pt-4 sm:grid-cols-2">
            {scenarios.map((s) => {
              const Icon = ICONS[s.key] ?? Globe;
              const active = (actionData && "picked" in actionData && actionData.picked === s.key) || pending === s.key;
              return (
                <button key={s.key} name="scenario" value={s.key} disabled={busy}
                        className={`group flex items-start gap-3 rounded-lg border p-4 text-left transition-colors disabled:opacity-70 ${active ? "border-[var(--brand)] bg-[var(--brand-bg)]" : "border-[var(--line)] hover:border-[var(--ink)]"}`}>
                  <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${active ? "bg-[var(--brand)] text-white" : "bg-[var(--chip)] text-[var(--ink-2)]"}`}>
                    {pending === s.key ? <Loader2 size={18} className="animate-spin" /> : <Icon size={18} />}
                  </span>
                  <span><span className="block font-semibold">{s.title}</span><span className="mt-0.5 block text-[13px] text-[var(--ink-3)]">{BLURB[s.key]}</span></span>
                </button>
              );
            })}
          </Form>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader title="Upload your usage" subtitle="CSV or JSON, up to 1 MB" />
          <Form method="post" encType="multipart/form-data" ref={formRef} className="p-6 pt-4">
            <label onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
                   onDrop={(e) => { e.preventDefault(); setDrag(false); if (e.dataTransfer.files[0] && fileRef.current) { fileRef.current.files = e.dataTransfer.files; setFileName(e.dataTransfer.files[0].name); } }}
                   className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors ${drag ? "border-[var(--brand)] bg-[var(--brand-bg)]" : "border-[var(--line-strong)] hover:border-[var(--ink)]"}`}>
              <FileUp size={26} className="text-[var(--ink-3)]" />
              <span className="text-sm font-semibold">{fileName || "Drop a file here or click to choose"}</span>
              <span className="text-xs text-[var(--ink-3)]">usage_type, quantity, unit, roaming_zone</span>
              <input ref={fileRef} type="file" name="file" accept=".csv,.json" className="sr-only" onChange={(e) => setFileName(e.target.files?.[0]?.name ?? "")} />
            </label>
            <div className="mt-4 flex items-center justify-between gap-3">
              <a href="/api/sample.csv" download="usage-sample.csv" className="text-sm font-semibold text-[var(--ink)] underline underline-offset-4">Download sample</a>
              <button disabled={busy || !fileName} className="inline-flex h-11 items-center gap-2 rounded-lg bg-[var(--brand)] px-5 font-bold text-white hover:bg-[var(--brand-hover)] disabled:opacity-50">
                {pending === "file" ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />} Analyse
              </button>
            </div>
          </Form>
        </Card>
      </div>


      <Card>
        <CardHeader title="Build your own" subtitle="Your own numbers, priced on your plan. Your real bill is never affected." />
        <Form method="post" className="space-y-5 p-6 pt-4">
          <input type="hidden" name="custom" value="1" />
          <fieldset className="flex flex-wrap gap-2" aria-label="Start from">
            {([["usual", "On top of my usual month"], ["scratch", "Start from zero"]] as const).map(([v, label]) => (
              <label key={v} className={`cursor-pointer rounded-full border px-4 py-2 text-sm font-semibold ${start === v ? "border-[var(--brand)] bg-[var(--brand-bg)]" : "border-[var(--line)] hover:border-[var(--ink)]"}`}>
                <input type="radio" name="start" value={v} checked={start === v} onChange={() => setStart(v)} className="sr-only" />{label}
              </label>
            ))}
          </fieldset>
          <p className="-mt-2 text-[13px] text-[var(--ink-3)]">{start === "usual" ? "What you enter is added to your real usage last month." : "What you enter is the whole month."}</p>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Num name="data_gb" label="Data at home" unit="GB" max={500} step="0.5" />
            <Num name="voice_min" label="Calls at home" unit="min" max={10000} />
            <Num name="intl_min" label="Calls abroad from home" unit="min" max={5000} />
            <Num name="sms" label="Text messages" unit="SMS" max={5000} />
          </div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <ZoneSelect />
            <Num name="trip_data_gb" label="Data while travelling" unit="GB" max={200} step="0.5" />
            <Num name="trip_voice_min" label="Calls while travelling" unit="min" max={5000} />
            <div className="flex items-end">
              <button disabled={busy} className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--brand)] px-5 font-bold text-white hover:bg-[var(--brand-hover)] disabled:opacity-50">
                {pending === "custom" ? <Loader2 size={16} className="animate-spin" /> : <Calculator size={16} />} Price it
              </button>
            </div>
          </div>
        </Form>
      </Card>

      {actionData && "error" in actionData && <div role="alert" className="rounded-lg bg-[var(--bad-bg)] px-4 py-3 text-sm font-medium text-[var(--bad)]">{actionData.error}</div>}

      {r && (
        <section className="fade-up space-y-6" aria-live="polite">
          <Card>
            <div className="flex flex-wrap items-end justify-between gap-6 p-6">
              <div>
                <p className="text-sm font-semibold text-[var(--ink-3)]">Estimated bill · {r.scenario_title ?? r.source.replace(/^upload:/, "file: ")}</p>
                <p className="display tabular mt-1 text-[44px] font-extrabold leading-none">{money(r.bill.total, r.bill.currency, loc)}</p>
                <p className="mt-2 text-[13px] text-[var(--ink-3)]">incl. VAT{r.baseline && r.compared_to ? <> · your actual {cycleLabel(r.compared_to, loc)} usage (from your network usage records) with this scenario</> : r.compared_to && <> · compared with your {cycleLabel(r.compared_to, loc)} bill</>}</p>
              </div>
              <DeltaPill ex={r.explanation} locale={loc} />
            </div>
          </Card>
          <ExplanationSection data={{ explanation: r.explanation, partial: false, llm: r.llm, sources: r.sources, status: r.status }}
                              lang="en" basePath="/insights" heading="What this would mean" showLanguages={false} />
        </section>
      )}
    </div>
  );
}

function Num({ name, label, unit, max, step = "1" }: { name: string; label: string; unit: string; max: number; step?: string }) {
  return (
    <label className="block text-sm font-semibold">{label}
      <span className="mt-1 flex h-11 items-center rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] focus-within:border-[var(--ink)]">
        <input type="number" inputMode="decimal" name={name} min={0} max={max} step={step} placeholder="0"
               className="tabular h-full w-full min-w-0 rounded-l-lg bg-transparent px-3 font-normal outline-none" />
        <span className="px-3 text-[13px] font-normal text-[var(--ink-3)]">{unit}</span>
      </span>
    </label>
  );
}

const ZONES = [
  { value: "", label: "Not travelling", sub: "Only usage at home" },
  { value: "EU", label: "EU", sub: "Roam like at home" },
  { value: "Z2", label: "Zone 2", sub: "UK, Switzerland, US …" },
  { value: "Z3", label: "Zone 3", sub: "Rest of the world" },
];

/** A styled listbox instead of a native <select>: the native popup is drawn by the OS (blue highlight, light
 *  menu on a dark page) and can't be themed. Keyboard: Enter/Space/↓ open, ↑/↓ move, Enter select, Esc close. */
function ZoneSelect() {
  const [value, setValue] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const current = ZONES.find((z) => z.value === value) ?? ZONES[0];

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  function pick(i: number) { setValue(ZONES[i].value); setOpen(false); }
  function onKey(e: React.KeyboardEvent) {
    if (!open && ["Enter", " ", "ArrowDown", "ArrowUp"].includes(e.key)) {
      e.preventDefault(); setActive(Math.max(0, ZONES.findIndex((z) => z.value === value))); setOpen(true); return;
    }
    if (!open) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(ZONES.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(active); }
    else if (e.key === "Escape" || e.key === "Tab") setOpen(false);
  }

  return (
    <div ref={ref} className="relative text-sm font-semibold">
      <span id={`${id}-l`}>Travelling to</span>
      <input type="hidden" name="trip_zone" value={value} />
      <button type="button" aria-haspopup="listbox" aria-expanded={open} aria-labelledby={`${id}-l ${id}-v`}
              aria-controls={`${id}-list`} onClick={() => { setActive(Math.max(0, ZONES.findIndex((z) => z.value === value))); setOpen((o) => !o); }}
              onKeyDown={onKey}
              className={`mt-1 flex h-11 w-full items-center justify-between gap-2 rounded-lg border bg-[var(--surface)] px-3 text-left font-normal transition-colors ${open ? "border-[var(--ink)]" : "border-[var(--line-strong)] hover:border-[var(--ink-3)] focus-visible:border-[var(--ink)]"}`}>
        <span id={`${id}-v`} className="truncate">{current.label}{current.value && <span className="text-[var(--ink-3)]"> · {current.sub}</span>}</span>
        <ChevronDown size={16} className={`shrink-0 text-[var(--ink-3)] transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <ul id={`${id}-list`} role="listbox" aria-labelledby={`${id}-l`}
            className="absolute left-0 right-0 z-30 mt-1 overflow-hidden rounded-lg border border-[var(--line-strong)] bg-[var(--surface)] p-1 font-normal shadow-[0_12px_32px_rgba(0,0,0,0.18)]">
          {ZONES.map((z, i) => {
            const selected = z.value === value;
            return (
              <li key={z.value || "none"} role="option" aria-selected={selected}
                  onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(i); }}
                  className={`flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 ${i === active ? "bg-[var(--chip)]" : ""}`}>
                <span className="min-w-0 flex-1">
                  <span className={`block ${selected ? "font-semibold" : ""}`}>{z.label}</span>
                  <span className="block text-[12.5px] text-[var(--ink-3)]">{z.sub}</span>
                </span>
                {selected && <Check size={16} className="shrink-0 text-[var(--brand)]" />}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
