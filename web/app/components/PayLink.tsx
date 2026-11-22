import { CreditCard } from "lucide-react";
import { useDegraded } from "~/routes/_app";
import { LinkButton } from "./ui";

/** Every "Pay" entry point (Overview, bill page, assistant) leads to the one payment page:
 *  /pay/bill/<cycle> for an issued bill, /pay/advance for this month's unbilled usage.
 *  While the payments service is down (open circuit) a notice replaces the link. */
export function PayLink({ purpose, cycle, label, variant = "primary", full = false }:
  { purpose: "BILL" | "ADVANCE"; cycle?: string; label: string; variant?: "primary" | "secondary"; full?: boolean }) {
  const paymentsDown = useDegraded("payments");
  if (paymentsDown) return <PaymentsDown />;
  const to = purpose === "BILL" && cycle ? `/pay/bill/${cycle}` : "/pay/advance";
  return (
    <LinkButton to={to} variant={variant} className={full ? "w-full" : ""}>
      <CreditCard size={16} /><span>{label}</span>
    </LinkButton>
  );
}

export function PaymentsDown() {
  return (
    <p role="status" className="max-w-[300px] rounded-lg border border-[var(--line-strong)] bg-[var(--chip)] px-3 py-2 text-[13px] text-[var(--ink-2)]">
      Payments are briefly unavailable. Your bill and due date are not affected; please try again in a few minutes.
    </p>
  );
}
