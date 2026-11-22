import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";

/** Subscribes to server-sent events; a new usage alert pops a toast and refreshes the snapshot. */
export function LiveNotifications() {
  const [toast, setToast] = useState<{ title: string; body: string } | null>(null);
  const revalidator = useRevalidator();
  useEffect(() => {
    // The browser gives up on an EventSource after a non-200 answer (e.g. during a core-api blip), so we
    // reconnect ourselves with backoff; otherwise live alerts stopped until a full reload.
    let es: EventSource | null = null, timer: ReturnType<typeof setTimeout> | undefined, delay = 2000, closed = false;
    const connect = () => {
      es = new EventSource("/api/events");
      es.onopen = () => { delay = 2000; };
      es.onerror = () => {
        if (closed || es?.readyState !== EventSource.CLOSED) return;   // CONNECTING = the browser retries itself
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 60_000);
      };
      es.addEventListener("notification", (e) => {
        try {
          const n = JSON.parse((e as MessageEvent).data);
          setToast({ title: n.title, body: n.body });
          revalidator.revalidate();
          setTimeout(() => setToast(null), 8000);
        } catch {}
      });
    };
    connect();
    return () => { closed = true; clearTimeout(timer); es?.close(); };
  }, []);
  if (!toast) return null;
  return (
    <div role="status" className="fixed bottom-20 right-4 z-[60] max-w-sm rounded-2xl border border-[var(--line)] bg-[var(--card)] p-4 pr-10 shadow-2xl md:bottom-4">
      <button type="button" onClick={() => setToast(null)} aria-label="Dismiss" className="absolute right-2 top-2 rounded-md px-2 py-1 text-[var(--ink-3)] hover:bg-[var(--chip)]">×</button>
      <p className="font-semibold text-[var(--ink)]">🔔 {toast.title}</p>
      <p className="mt-1 text-sm text-[var(--ink-2)]">{toast.body}</p>
    </div>
  );
}
