import { useEffect } from "react";
import { toast } from "./Toast";

/**
 * Loads the new version of the site after a deployment, even in a tab that is already open.
 *
 * Detect: every response from our server carries `X-App-Version` (the build's manifest hash). Any
 * same-origin fetch (page data on navigation, /api calls) is checked, plus a light `/version` poll every
 * 60 s while the tab is visible and whenever it regains focus.
 * Act: once outdated, the next click on any link or button does a full load of the new version (the
 * link's target, or this page for a button) instead of running old code. The user clicks again on
 * the new version; nothing is lost - all state lives on the server.
 * Rolling deploys: old and new pods serve side by side for a few seconds. A version this tab has
 * already loaded is never a reason to reload (tracked per tab), so the tab can't bounce between them.
 */
const SEEN = "app-versions-seen";
const UPDATED = "app-updated";

function read(key: string): string[] {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "[]"); } catch { return []; }
}

export function VersionWatcher() {
  useEffect(() => {
    const mine = (window as any).__reactRouterManifest?.version as string | undefined;
    if (!mine) return;
    const seen = new Set([...read(SEEN), mine]);
    try { sessionStorage.setItem(SEEN, JSON.stringify([...seen].slice(-10))); } catch { /* private mode */ }
    try {
      if (sessionStorage.getItem(UPDATED)) { sessionStorage.removeItem(UPDATED); toast("You're on the latest version of the site.", "good"); }
    } catch { /* private mode */ }

    let outdated = false;
    const note = (v: string | null) => { if (v && !seen.has(v)) outdated = true; };

    const origFetch = window.fetch;
    window.fetch = async (...args) => {
      const r = await origFetch(...args);
      if (r.url.startsWith(location.origin)) note(r.headers.get("x-app-version"));
      return r;
    };
    const check = () => {
      if (document.visibilityState !== "visible") return;
      origFetch("/version", { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).then((j) => note(j?.version ?? null)).catch(() => {});
    };
    const timer = setInterval(check, 60_000);
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);

    const intercept = (e: Event) => {
      if (!outdated) return;
      const el = (e.target as Element | null)?.closest?.("a[href], button, [role=button], input[type=submit]");
      // in-page tabs (e.g. the explanation's language) only fetch data the old page handles fine: no reload for
      // them (it looked like the language switch refreshed the page); the next real navigation loads the new version
      if (!el || el.closest("[role=tab], [role=tablist]")) return;
      const a = el instanceof HTMLAnchorElement ? el : null;
      if (a && (a.target === "_blank" || a.hasAttribute("download") || new URL(a.href, location.href).origin !== location.origin)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      try { sessionStorage.setItem(UPDATED, "1"); } catch { /* private mode */ }
      if (a) location.assign(a.href); else location.reload();
    };
    document.addEventListener("click", intercept, true);   // capture: runs before React's handlers
    document.addEventListener("submit", intercept, true);
    // An old tab asking for a code chunk that the new deployment no longer serves: load the new version.
    const preloadError = (e: Event) => { e.preventDefault(); location.reload(); };
    window.addEventListener("vite:preloadError", preloadError);

    return () => {
      window.fetch = origFetch;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("focus", check);
      document.removeEventListener("click", intercept, true);
      document.removeEventListener("submit", intercept, true);
      window.removeEventListener("vite:preloadError", preloadError);
    };
  }, []);
  return null;
}
