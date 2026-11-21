import { CheckCircle2, CircleDashed, Wrench, XCircle } from "lucide-react";
import type { AgentTrace as T } from "~/lib/types";

const usd = (v: number) => (v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);

/** "How this answer was made": every stage of the agent, with scores, verdicts, tokens and cost. */
export function AgentTraceView({ trace }: { trace: T }) {
  const r = trace.retrieval;
  return (
    <div className="space-y-4 text-[13px]">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-[var(--ink-3)]">
        {trace.totals && <span>cost <b className="text-[var(--ink)]">{usd(trace.totals.cost_usd)}</b></span>}
        {trace.totals && <span>LLM calls <b className="text-[var(--ink)]">{trace.totals.llm_calls}</b></span>}
        {trace.totals && <span>tokens <b className="text-[var(--ink)]">{trace.totals.prompt_tokens + trace.totals.completion_tokens}</b></span>}
        {trace.totals?.cache_hit_ratio != null && <span>prompt-cache hit <b className="text-[var(--ink)]">{Math.round(trace.totals.cache_hit_ratio * 100)}%</b></span>}
        {trace.latency_ms != null && <span>latency <b className="text-[var(--ink)]">{(trace.latency_ms / 1000).toFixed(1)}s</b></span>}
        {trace.cache && <span>answer cache <b className="text-[var(--ink)]">{trace.cache}</b></span>}
      </div>

      {trace.spine && (
        <p className="text-xs text-[var(--ink-3)]">
          <b className="text-[var(--ink-2)]">L1 context spine:</b> {trace.spine.subscriber.docs} customer documents + {trace.spine.shared.docs} shared knowledge documents
          {" "}({trace.spine.subscriber.embedded + trace.spine.shared.embedded} re-embedded on this request with local MiniLM).
        </p>
      )}

      {r && (
        <div>
          <p className="mb-1.5 text-xs text-[var(--ink-3)]">
            <b className="text-[var(--ink-2)]">L2 hybrid retrieval</b> over {r.candidates} documents · rankers {r.rankers.join(" + ")} fused with RRF · vectors: {r.vector_engine}
            {r.target_cycle && <> · cycle {r.target_cycle}</>} · grader: {r.grader.sufficient ? "context sufficient" : `insufficient${r.grader.missing ? ` (${r.grader.missing})` : ""}`}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead className="text-left text-[10px] uppercase tracking-wide text-[var(--ink-3)]">
                <tr><th className="py-1">Doc</th><th className="py-1">Title</th><th className="py-1 text-right">BM25</th><th className="py-1 text-right">Vector</th><th className="py-1 text-right">RRF</th><th className="py-1 text-center">Grader</th></tr>
              </thead>
              <tbody className="tabular divide-y divide-[var(--line)]">
                {r.results.map((x) => (
                  <tr key={x.id}>
                    <td className="py-1 pr-2 font-bold">{x.label}</td>
                    <td className="py-1 pr-2">{x.title} <span className="text-[var(--ink-3)]">({x.type})</span></td>
                    <td className="py-1 text-right">{x.scores.bm25}</td>
                    <td className="py-1 text-right">{x.scores.vector}</td>
                    <td className="py-1 text-right">{x.scores.rrf}</td>
                    <td className="py-1 text-center">{x.relevant ? <CheckCircle2 size={14} className="inline text-[var(--good)]" /> : <CircleDashed size={14} className="inline text-[var(--ink-3)]" />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!!trace.corrective?.length && (
        <div>
          <p className="mb-1 text-xs font-bold text-[var(--ink-2)]">CRAG corrective actions</p>
          <ul className="space-y-0.5 text-xs text-[var(--ink-3)]">
            {trace.corrective.map((c, i) => (
              <li key={i} className="flex gap-1.5"><Wrench size={12} className="mt-0.5 shrink-0" /> {c.action} {JSON.stringify(Object.fromEntries(Object.entries(c).filter(([k]) => k !== "action")))}</li>
            ))}
          </ul>
        </div>
      )}

      {!!trace.attempts?.length && (
        <div>
          <p className="mb-1 text-xs font-bold text-[var(--ink-2)]">Verification</p>
          <ul className="space-y-0.5 text-xs">
            {trace.attempts.map((a) => (
              <li key={a.attempt} className="flex items-start gap-1.5">
                {a.verified ? <CheckCircle2 size={13} className="mt-px shrink-0 text-[var(--good)]" /> : <XCircle size={13} className="mt-px shrink-0 text-[var(--bad)]" />}
                <span>attempt {a.attempt}: {a.verified ? "every number found in the cited documents" : a.problems.join("; ")}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!!trace.stages?.length && (
        <div>
          <p className="mb-1 text-xs font-bold text-[var(--ink-2)]">LLM calls: prompt cache and spend per stage</p>
          <table className="w-full text-[12px]">
            <thead className="text-left text-[10px] uppercase tracking-wide text-[var(--ink-3)]">
              <tr><th className="py-1">Stage</th><th className="py-1 text-right">In (cache hit)</th><th className="py-1 text-right">Out</th><th className="py-1 text-right">Cost</th><th className="py-1 text-right">Time</th></tr>
            </thead>
            <tbody className="tabular divide-y divide-[var(--line)]">
              {trace.stages.map((s, i) => (
                <tr key={i}>
                  <td className="py-1">{s.stage}</td>
                  <td className="py-1 text-right">{s.prompt_tokens} ({s.cache_hit_tokens})</td><td className="py-1 text-right">{s.completion_tokens}</td>
                  <td className="py-1 text-right">{usd(s.cost_usd)}</td><td className="py-1 text-right">{s.seconds.toFixed(1)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!trace.degraded?.length && <p className="text-xs text-[var(--warn)]">Degraded: {trace.degraded.join(" · ")}</p>}
    </div>
  );
}
