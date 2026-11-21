export type BillSummary = {
  id: string; cycle: string; total: number; subtotal: number; vat: number; currency: string;
  status: "PAID" | "ISSUED"; amount_paid: number; outstanding: number; issued_at: string; due_date: string;
};
export type Line = { code: string; category: string; label: string; quantity: number; unit: string; amount: number };
export type Driver = { code: string; label: string; category: string; previous: number; current: number; delta: number; detail: string };
export type UsageDriver = { code: string; label: string; amount: number; share_pct: number; detail: string };
export type Explanation = {
  cycle: string; previous_cycle: string | null; currency: string; previous_total: number | null; current_total: number;
  delta_total: number | null; delta_pct: number | null; direction: "UP" | "DOWN" | "SAME" | "FIRST_BILL";
  drivers: Driver[]; top_usage_drivers: UsageDriver[]; usage_changes: string[]; facts: string[];
};
export type Citation = { label: string; id: string; title: string; doc_type: string; cycle: string | null; source_ref: string; body: string };
export type AgentTrace = {
  prompt_version?: string; cache?: string; latency_ms?: number; degraded?: string[];
  spine?: { subscriber: { docs: number; embedded: number }; shared: { docs: number; embedded: number } };
  retrieval?: { candidates: number; vector_engine: string; target_cycle: string | null; rankers: string[];
    results: { label: string; id: string; title: string; type: string; relevant: boolean; scores: Record<string, number | null> }[];
    grader: { sufficient: boolean; missing: string } };
  corrective?: { action: string; [k: string]: unknown }[];
  attempts?: { attempt: number; verified: boolean; problems: string[] }[];
  stages?: { stage: string; model: string; prompt_tokens: number; completion_tokens: number; cache_hit_tokens: number; cache_miss_tokens: number; cost_usd: number; seconds: number }[];
  totals?: { cost_usd: number; prompt_tokens: number; completion_tokens: number; cache_hit_tokens: number; cache_hit_ratio: number | null; llm_calls: number };
};
export type ExplanationResponse = { explanation: Explanation; narrative: string; provider: string; partial: boolean; status?: string;
  citations?: Citation[]; trace?: AgentTrace; bill_id?: string | null };
export type AgentAnswer = { status: string; answer: string; citations: Citation[]; provider: string; trace: AgentTrace };
export type Evidence = {
  subscriber_id: string; cycle: string; partition: number; source: string; objects_scanned: number; cdr_count: number;
  first_event: string | null; last_event: string | null; all_match: boolean; note: string | null; bill_id: string | null;
  reconciliation: { key: string; unit: string; cdr_records: number; from_cdrs: number; valkey_counter: number; match: boolean }[];
  sample: { record_id: string; event_time: string; key: string; quantity: number; unit: string; archive_object: string }[];
  bill_lines: Line[];
};
export type Home = {
  as_of: string;
  subscriber: { id: string; name: string; email?: string; country: string; locale: string };
  plan: { code: string; name: string; monthly_fee: number; data_mb: number; voice_min: number | null; sms: number | null; overage_policy: string };
  usage: { cycle: string; data_pct: number; version: number | null; data_mb: number; data_allowance_mb: number; voice_min: number; sms: number;
           intl_voice_min: number; roaming_data_mb: Record<string, number>; roaming_voice_min: Record<string, number>; eu_roaming_data_mb: number };
  unbilled: { cycle: string; currency: string; subtotal: number; vat: number; total: number; lines: Line[] };
  outstanding: { amount: number; currency: string; bill_id: string | null };
  last_bill: BillSummary | null;
  trend: BillSummary[];
  notifications: { id: number; kind: string; title: string; body: string; created_at: string; read: boolean }[];
  features: { ai_explanations: boolean; payments: boolean };
};
