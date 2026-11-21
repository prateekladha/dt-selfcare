import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";

/** Subscribes to server-sent events; a new usage alert pops a toast and refreshes the snapshot. */
export function LiveNotifications() {
  const [toast, setToast] = useState<{ title: string; body: string } | null>(null);
  const revalidator = useRevalidator();
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.addEventListener("notification", (e) => {
      try {
        const n = JSON.parse((e as MessageEvent).data);
        setToast({ title: n.title, body: n.body });
        revalidator.revalidate();
        setTimeout(() => setToast(null), 8000);
      } catch {}
    });
    return () => es.close();
  }, []);
  if (!toast) return null;
  return (
    <div role="status" className="fixed bottom-4 right-4 z-50 max-w-sm rounded-2xl border border-[var(--line)] bg-[var(--card)] p-4 shadow-2xl">
      <p className="font-semibold text-[var(--ink)]">🔔 {toast.title}</p>
      <p className="mt-1 text-sm text-[var(--ink-2)]">{toast.body}</p>
    </div>
  );
}
