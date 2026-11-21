"""Bill PDF: rendered from a pure InvoiceContext; text is checked in uncompressed content streams."""
import re
from datetime import UTC, datetime, timedelta

from core_api.services.pdf import InvoiceContext, PaymentEntry, format_msisdn, money, render_bill_pdf
from telco_common.billing import rate
from telco_common.catalog import MB, get_plan

DE_SUB, PL_SUB = "4915100000002", "48600000001"


def text_of(pdf: bytes) -> str:
    """Concatenate the literal strings of all text operators (works with compress=False)."""
    raw = pdf.decode("cp1252", errors="replace")  # standard fonts use WinAnsi
    parts = re.findall(r"\((?:\\.|[^\\)])*\)\s*Tj", raw)
    out = []
    for p in parts:
        s = p[1:p.rindex(")")]
        s = re.sub(r"\\([()\\])", r"\1", s)
        s = re.sub(r"\\(\d{3})", lambda m: bytes([int(m.group(1), 8)]).decode("cp1252", errors="replace"), s)
        out.append(s)
    return "\n".join(out)


def ctx_for(bill, prev=None, paid=0, payments=(), name="Jonas Weber", country="DE", email="jonas@example.com"):
    issued = datetime(2026, 11, 1, tzinfo=UTC)
    return InvoiceContext(bill=bill, invoice_number=f"{bill.subscriber_id}:{bill.cycle}", issued_at=issued,
                          due_date=issued + timedelta(days=14), amount_paid=paid, customer_name=name,
                          msisdn=bill.subscriber_id, country=country, email=email, previous_bill=prev,
                          payments=list(payments))


def check_pdf(pdf: bytes) -> str:
    assert pdf.startswith(b"%PDF") and len(pdf) > 1024
    return text_of(pdf)


def test_german_bill_with_roaming_addon_previous_bill_and_payment():
    plan = get_plan("DE-FLEX")
    prev = rate(DE_SUB, "2026-09", plan, {"DATA": 1_000 * MB, "VOICE": 3_000})
    bill = rate(DE_SUB, "2026-10", plan, {
        "DATA": 6_500 * MB, "ROAMING_DATA:EU": 300 * MB, "ROAMING_DATA:Z2": 250 * MB, "ROAMING_VOICE:Z2": 600,
        "ROAMING_VOICE_MT:Z2": 300, "addon:DE-DATA-5GB": 1, "VOICE": 15_000, "SMS": 120, "INTL_VOICE": 900,
        "MMS": 2, "PREMIUM": 499, "EMERGENCY": 60})
    pay = PaymentEntry(1000, datetime(2026, 11, 5, tzinfo=UTC), "In-app (razorpay)", "pay_ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    text = check_pdf(render_bill_pdf(ctx_for(bill, prev, paid=1000, payments=[pay]), compress=False))
    for s in ["Self-care", "Invoice", f"{DE_SUB}:2026-10", "Jonas Weber", "+49 151 0000 0002", "VAT 19%",
              "Roaming data, zone Z2", "Roaming calls, zone Z2", "5 GB data add-on", "Add-ons", "Roaming",
              "Why this bill changed", "Partially paid", "1 Oct 2026 \u2013 31 Oct 2026", "15 Nov 2026",
              "pay_ABCDEFGHIJKL", "Usage summary", "Unlimited" if plan.voice_min is None else "200 min",
              "Page 1 of", "CDRs", "Mobile Flex", money(bill.total, "EUR"), "Change vs Sep 2026"]:
        assert s in text, s
    assert "Telekom" not in text and "T-Mobile" not in text


def test_polish_bill_without_previous_bill_or_payments():
    bill = rate(PL_SUB, "2026-10", get_plan("PL-FLEX"), {"DATA": 2_000 * MB, "INTL_VOICE": 1_200,
                                                         "ROAMING_DATA:Z3": 20 * MB, "SMS_INTL": 3})
    pdf = render_bill_pdf(ctx_for(bill, name="Zofia Now\u0105k-\u0141ukasz", country="PL", email=None),
                          compress=False)
    text = check_pdf(pdf)
    for s in [f"{PL_SUB}:2026-10", "Zofia Nowak-Lukasz", "+48 600 000 001", "Poland", "VAT 23%", "PLN",
              "Roaming data, zone Z3", "International calls", "No payments yet", "first invoice", "Due",
              "Page 1 of", "Amounts in PLN"]:
        assert s in text, s


def test_prorated_plan_lines_and_paid_bill():
    segs = [(get_plan("DE-S"), 10), (get_plan("DE-FLEX"), 20)]
    bill = rate(DE_SUB, "2026-11", get_plan("DE-FLEX"), {"DATA": 500 * MB}, segments=segs)
    text = check_pdf(render_bill_pdf(ctx_for(bill, paid=bill.total), compress=False))
    for s in ["Mobile S monthly fee, 10 of 30 days", "Mobile Flex monthly fee, 20 of 30 days", "pro-rated",
              "\u20ac19.95/month", "Paid", "Direct debit", "paid in full", "Page 1 of", "VAT"]:
        assert s in text, s


def test_empty_bill_renders_and_is_deterministic():
    bill = rate(DE_SUB, "2026-10", get_plan("DE-M"), {}).model_copy(update={"lines": [], "subtotal": 0, "vat": 0,
                                                                            "total": 0})
    a, b = render_bill_pdf(ctx_for(bill)), render_bill_pdf(ctx_for(bill))
    assert a == b and a.startswith(b"%PDF")
    assert "No charges in this billing period" in text_of(render_bill_pdf(ctx_for(bill), compress=False))


def test_msisdn_formatting():
    assert format_msisdn("4915100000002") == "+49 151 0000 0002"
    assert format_msisdn("48600000001") == "+48 600 000 001"
    assert money(123456, "EUR") == "\u20ac1,234.56" and money(-500, "PLN") == "-5.00 PLN"


def test_endpoint_includes_ledger_payments(monkeypatch):
    from core_api.db import bill_payments, bills, create_schema, payments, set_engine, subscribers
    from core_api.routers import me
    from core_api.security import Principal
    from sqlalchemy import create_engine, insert
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_schema(engine)
    set_engine(engine)
    plan = get_plan("DE-FLEX")
    bill = rate(DE_SUB, "2026-10", plan, {"DATA": 1_000 * MB, "ROAMING_DATA:Z2": 100 * MB})
    issued = datetime(2026, 11, 1, tzinfo=UTC)
    with engine.begin() as cx:
        cx.execute(insert(subscribers).values(id=DE_SUB, name="Jonas Weber", email="j@example.com", country="DE",
                                              plan_code="DE-FLEX", password_hash="x"))
        cx.execute(insert(bills).values(id=f"{DE_SUB}:2026-10", subscriber_id=DE_SUB, cycle="2026-10",
                                        plan_code="DE-FLEX", currency="EUR", subtotal=bill.subtotal, vat=bill.vat,
                                        total=bill.total, amount_paid=500, status="ISSUED", issued_at=issued,
                                        due_date=issued + timedelta(days=14), payload=bill.model_dump_json()))
        cx.execute(insert(payments).values(id="p-1", subscriber_id=DE_SUB, purpose="BILL", bill_id=f"{DE_SUB}:2026-10",
                                           amount=500, currency="EUR", gateway="razorpay", status="PAID",
                                           gateway_payment_id="pay_XYZ123", idempotency_key="k1"))
        cx.execute(insert(bill_payments).values(payment_id="p-1", bill_id=f"{DE_SUB}:2026-10", amount=500,
                                                applied_at=datetime(2026, 11, 3, tzinfo=UTC)))
    calls = {}
    real = me.render_bill_pdf
    monkeypatch.setattr(me, "render_bill_pdf", lambda ctx: calls.setdefault("ctx", ctx) and real(ctx))
    resp = me.bill_pdf("2026-10", Principal(DE_SUB, "DE"))
    assert resp.body.startswith(b"%PDF")
    ctx = calls["ctx"]
    assert ctx.previous_bill is None and ctx.amount_paid == 500 and ctx.outstanding == bill.total - 500
    assert [(p.amount, p.reference, p.method) for p in ctx.payments] == [(500, "pay_XYZ123", "In-app (razorpay)")]
    assert "pay_XYZ123" in text_of(render_bill_pdf(ctx, compress=False))
