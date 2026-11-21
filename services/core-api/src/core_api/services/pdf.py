"""Bill PDF rendering (deterministic; can be pre-generated to Object Storage on bill day)."""
from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from telco_common.billing import Bill
from telco_common.explainer import fmt_money

MAGENTA = colors.HexColor("#E20074")


def render_bill_pdf(bill: Bill, customer_name: str, status: str, due_date: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Bill {bill.cycle}", leftMargin=40, rightMargin=40)
    st = getSampleStyleSheet()
    cur = bill.currency
    rows = [["Item", "Quantity", "Amount"]]
    rows += [[line.label, f"{line.quantity:,} {line.unit}", fmt_money(line.amount, cur)] for line in bill.lines]
    rows += [["Subtotal", "", fmt_money(bill.subtotal, cur)],
             [f"VAT {float(bill.vat_rate) * 100:.0f}%", "", fmt_money(bill.vat, cur)],
             ["Total", "", fmt_money(bill.total, cur)]]
    table = Table(rows, colWidths=[270, 120, 110])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), MAGENTA), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("LINEABOVE", (0, -3), (-1, -3), 0.5, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    u = bill.usage
    story = [
        Paragraph("<b>Mobile bill</b>", st["Title"]),
        Paragraph(f"{customer_name} · Line {bill.subscriber_id} · Cycle {bill.cycle}", st["Normal"]),
        Paragraph(f"Plan {bill.plan_code} · Status {status} · Due {due_date}", st["Normal"]),
        Spacer(1, 16), table, Spacer(1, 16),
        Paragraph(f"Usage: {u.data_mb:,} MB of {u.data_allowance_mb:,} MB data, {u.voice_min:,} min calls, "
                  f"{u.sms:,} SMS, {u.intl_voice_min:,} min international.", st["Normal"]),
    ]
    doc.build(story)
    return buf.getvalue()
