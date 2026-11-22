"""Bill PDF rendering (deterministic; can be pre-generated to Object Storage on bill day).

Pure: the caller gathers everything into an `InvoiceContext` (bill, previous bill, customer, payments) and
this module only lays it out. No network, no clock: the same context always renders the same bytes
(reportlab invariant mode), so a re-download matches the invoice the customer saw on bill day.

Text uses an EMBEDDED TrueType font (DejaVu Sans, bundled in core_api/assets/fonts with its licence), so every
viewer draws exactly the glyphs and widths laid out here. The standard Helvetica is not embedded: viewers
substituted their own font and its "€" overlapped the following digit. DejaVu covers Latin Extended-A, so Polish
names and addresses print correctly (ą ę ń ś ź ż ł); anything still missing is transliterated. PLN amounts are
written with the ISO code.
"""
from __future__ import annotations

import unicodedata
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from telco_common.billing import Bill, BillLine, LineCategory
from telco_common.catalog import Plan, get_plan
from telco_common.explainer import explain

MAGENTA = colors.HexColor("#E20074")
MAGENTA_TINT = colors.HexColor("#FCE6F1")
INK = colors.HexColor("#1F2328")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#E5E7EB")
PANEL = colors.HexColor("#F6F7F9")
GREEN = colors.HexColor("#1A7F37")
AMBER = colors.HexColor("#B45309")

PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X

COUNTRY_NAMES = {"DE": "Germany", "PL": "Poland"}
CATEGORY_TITLES = {
    LineCategory.PLAN: "Plan and tariff",
    LineCategory.ADDON: "Add-ons",
    LineCategory.OVERAGE: "Usage beyond allowance",
    LineCategory.ROAMING: "Roaming",
    LineCategory.INTERNATIONAL: "International",
    LineCategory.MESSAGING: "Messaging",
    LineCategory.THIRD_PARTY: "Third-party and premium services",
    LineCategory.ADJUSTMENT: "Adjustments and corrections",
}


@dataclass(frozen=True)
class PaymentEntry:
    amount: int  # minor units, bill currency
    paid_at: datetime | None = None
    method: str = ""
    reference: str = ""


@dataclass(frozen=True)
class Seller:
    name: str
    address: str
    vat_id: str


# The seller block a VAT invoice needs (§14 UStG / Polish VAT act): name, address, VAT ID. Fictional demo operator
# per market: no real company, address, registration or trademark. Production: from the operator's configuration.
DEMO_SELLERS = {
    "DE": Seller("Self-care Mobile GmbH (demo operator)", "Beispielstraße 1, 10115 Berlin, Germany", "DE000000000 (demo)"),
    "PL": Seller("Self-care Mobile sp. z o.o. (demo operator)", "ul. Przykładowa 1, 00-001 Warszawa, Poland",
                 "PL0000000000 (demo)"),
}


@dataclass(frozen=True)
class InvoiceContext:
    bill: Bill
    invoice_number: str
    issued_at: datetime
    due_date: datetime
    amount_paid: int
    customer_name: str
    msisdn: str
    country: str
    email: str | None = None
    previous_bill: Bill | None = None
    payments: list[PaymentEntry] = field(default_factory=list)
    seller: Seller | None = None

    @property
    def seller_info(self) -> Seller:
        return self.seller or DEMO_SELLERS.get(self.country) or DEMO_SELLERS["DE"]

    @property
    def outstanding(self) -> int:
        return max(0, self.bill.total - self.amount_paid)

    @property
    def plan(self) -> Plan | None:
        try:
            return get_plan(self.bill.plan_code)
        except KeyError:
            return None


# ---------------------------------------------------------------- formatting helpers

def _register_fonts() -> tuple[str, str]:
    import os

    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    bundled = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
    if os.path.exists(os.path.join(bundled, "DejaVuSans.ttf")):
        regular, bold = os.path.join(bundled, "DejaVuSans.ttf"), os.path.join(bundled, "DejaVuSans-Bold.ttf")
    else:   # fallback: the Vera font inside reportlab (no Polish ą ę ń ś ź ż; those are transliterated)
        d = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
        regular, bold = os.path.join(d, "Vera.ttf"), os.path.join(d, "VeraBd.ttf")
    pdfmetrics.registerFont(TTFont("BillSans", regular))
    pdfmetrics.registerFont(TTFont("BillSans-Bold", bold))
    pdfmetrics.registerFontFamily("BillSans", normal="BillSans", bold="BillSans-Bold", italic="BillSans", boldItalic="BillSans-Bold")
    return "BillSans", "BillSans-Bold"


REGULAR, BOLD = _register_fonts()

# ...and make it reportlab's base font too: the canvas/page preamble and any style without an explicit
# fontName would otherwise reference the (non-embedded) Helvetica.
from reportlab import rl_config

rl_config.canvas_basefontname = REGULAR


def _table_style(cmds: list) -> TableStyle:
    """Tables default to Helvetica for any cell without an explicit FONTNAME: start every table with ours."""
    return TableStyle([("FONTNAME", (0, 0), (-1, -1), REGULAR), *cmds])


def _has_glyph(ch: str) -> bool:
    from reportlab.pdfbase import pdfmetrics
    return ord(ch) in pdfmetrics.getFont(REGULAR).face.charToGlyph


_TRANSLIT = str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ß": "ss"})


def safe(text: object) -> str:
    """Keep every character the embedded font can draw (umlauts, €, –, ł...); transliterate the rest."""
    out = []
    for ch in str(text):
        if ch in "\n\t" or _has_glyph(ch):
            out.append(ch)
        else:
            ch = ch.translate(_TRANSLIT)
            base = unicodedata.normalize("NFKD", ch).encode("cp1252", "ignore").decode("cp1252")
            out.append(base or "?")
    return "".join(out)


def _x(text: object) -> str:
    """Safe + XML-escaped, for Paragraph markup."""
    return escape(safe(text))


def money(minor: int, currency: str, signed: bool = False) -> str:
    sign = "-" if minor < 0 else ("+" if signed and minor > 0 else "")
    num = f"{abs(minor) / 100:,.2f}"
    if currency == "EUR":
        return f"{sign}€{num}"
    return f"{sign}{num} {currency}"


def rate_str(minor: Decimal, currency: str) -> str:
    """Per-unit price that may be fractional in minor units (e.g. 0.9 cent per MB)."""
    major = (minor / 100).quantize(Decimal("0.0001"))
    text = f"{major:,.4f}".rstrip("0")
    if len(text.split(".")[1]) < 2:
        text = f"{major:,.2f}"
    return f"€{text}" if currency == "EUR" else f"{text} {currency}"


def fmt_date(d: date | datetime | None) -> str:
    return f"{d.day} {d:%b %Y}" if d else "-"


def period_of(cycle: str) -> tuple[date, date]:
    y, m = map(int, cycle.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def format_msisdn(msisdn: str) -> str:
    d = "".join(c for c in msisdn if c.isdigit())
    if d.startswith("49") and len(d) >= 11:  # +49 151 0000 0002
        rest = d[5:]
        return f"+49 {d[2:5]} {rest[:4]} {rest[4:]}".rstrip()
    if d.startswith("48") and len(d) == 11:  # +48 600 000 001
        return f"+48 {d[2:5]} {d[5:8]} {d[8:]}"
    return f"+{d}" if d else msisdn


def fmt_mb(mb: int) -> str:
    return f"{mb:,} MB" + (f" ({mb / 1000:,.2f} GB)" if mb >= 1000 else "")


def status_of(ctx: InvoiceContext) -> tuple[str, colors.Color]:
    if ctx.bill.total <= 0:
        return "Nothing to pay", GREEN
    if ctx.outstanding == 0:
        return "Paid", GREEN
    if ctx.amount_paid > 0:
        return f"Partially paid\n{money(ctx.outstanding, ctx.bill.currency)} outstanding", AMBER
    return "Due", MAGENTA


# ---------------------------------------------------------------- styles

def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=REGULAR, bulletFontName=REGULAR, fontSize=9, leading=12.5, textColor=INK)
    s = {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=MUTED),
        "label": ParagraphStyle("label", parent=base, fontSize=7.5, leading=10, textColor=MUTED),
        "brand": ParagraphStyle("brand", parent=base, fontName=BOLD, fontSize=22, leading=26,
                                textColor=MAGENTA),
        "title": ParagraphStyle("title", parent=base, fontName=BOLD, fontSize=20, leading=24,
                                alignment=TA_RIGHT),
        "h2": ParagraphStyle("h2", parent=base, fontName=BOLD, fontSize=11.5, leading=15,
                             spaceBefore=14, spaceAfter=6),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.8, leading=11.5),
        "cell_r": ParagraphStyle("cell_r", parent=base, fontSize=8.8, leading=11.5, alignment=TA_RIGHT),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=2, spaceAfter=3),
        "big": ParagraphStyle("big", parent=base, fontName=BOLD, fontSize=22, leading=26,
                              textColor=INK),
    }
    return s


def _h2(text: str, st) -> Paragraph:
    return Paragraph(_x(text), st["h2"])


# ---------------------------------------------------------------- sections

def _header(ctx: InvoiceContext, st) -> list:
    start, end = period_of(ctx.bill.cycle)
    status, status_colour = status_of(ctx)
    seller = ctx.seller_info
    left = [Paragraph("Self-care", st["brand"]), Paragraph("Mobile services", st["small"]), Spacer(1, 4),
            Paragraph(f"{_x(seller.name)}<br/>{_x(seller.address)}<br/>VAT ID {_x(seller.vat_id)}", st["small"])]
    right = [Paragraph("Invoice", st["title"]),
             Paragraph(f"No. {_x(ctx.invoice_number)}", ParagraphStyle("r", parent=st["small"], alignment=TA_RIGHT))]
    top = Table([[left, right]], colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5])
    top.setStyle(_table_style([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                             ("LINEBELOW", (0, 0), (-1, 0), 2, MAGENTA)]))

    def meta(label: str, value: str, colour=INK, bold=False) -> list:
        vs = ParagraphStyle("v", parent=st["base"], textColor=colour,
                            fontName=BOLD if bold else REGULAR)
        return [Paragraph(_x(label.upper()), st["label"]), Paragraph(_x(value).replace("\n", "<br/>"), vs)]

    cells = [
        meta("Invoice number", ctx.invoice_number),
        meta("Issue date", fmt_date(ctx.issued_at)),
        meta("Billing period", f"{fmt_date(start)} – {fmt_date(end)}"),
        meta("Due date", fmt_date(ctx.due_date), bold=True),
        meta("Status", status, colour=status_colour, bold=True),
    ]
    widths = [0.25, 0.14, 0.27, 0.18, 0.16]
    row = Table([cells], colWidths=[CONTENT_W * w for w in widths])
    row.setStyle(_table_style([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 10)]))
    return [top, row, Spacer(1, 14)]


def _customer_and_summary(ctx: InvoiceContext, st) -> Table:
    b, cur, plan = ctx.bill, ctx.bill.currency, ctx.plan
    cust = [Paragraph("BILLED TO", st["label"]), Spacer(1, 3),
            Paragraph(f"<b>{_x(ctx.customer_name)}</b>", ParagraphStyle("n", parent=st["base"], fontSize=11,
                                                                       leading=15)),
            Paragraph(f"Mobile number: {_x(format_msisdn(ctx.msisdn))}", st["base"])]
    if ctx.email:
        cust.append(Paragraph(f"Email: {_x(ctx.email)}", st["base"]))
    cust.append(Paragraph(f"Country: {_x(COUNTRY_NAMES.get(ctx.country, ctx.country))}", st["base"]))
    cust += [Spacer(1, 10), Paragraph("TARIFF", st["label"]), Spacer(1, 3)]
    if plan:
        cust.append(Paragraph(f"<b>{_x(plan.name)}</b> ({_x(plan.code)})", st["base"]))
        cust.append(Paragraph(f"Monthly fee: {_x(money(plan.monthly_fee, cur))} excl. VAT", st["base"]))
    else:
        cust.append(Paragraph(_x(b.plan_code), st["base"]))
    if any(line.code.startswith("PLAN_FEE:") for line in b.lines):
        cust.append(Paragraph("Tariff changed during this period; fees are pro-rated by day.", st["small"]))

    vat_pct = f"{float(b.vat_rate) * 100:g}%"
    due_label = ("Amount due" if ctx.outstanding else "Amount due (nothing to pay)" if b.total <= 0
                 else "Amount due (paid in full)")
    rows = [
        [Paragraph(_x(due_label.upper()), st["label"]), ""],
        [Paragraph(_x(money(ctx.outstanding, cur)), st["big"]), ""],
        [Paragraph(f"by {_x(fmt_date(ctx.due_date))}" if ctx.outstanding
                   else "No payment is needed for this invoice." if b.total <= 0 else "Thank you for your payment.",
                   st["small"]), ""],
        ["Subtotal (net)", money(b.subtotal, cur)],
        [f"VAT {vat_pct}", money(b.vat, cur)],
        ["Invoice total", money(b.total, cur)],
        ["Amount paid", money(-ctx.amount_paid, cur) if ctx.amount_paid else money(0, cur)],
        ["Outstanding", money(ctx.outstanding, cur)],
    ]
    prev = ctx.previous_bill
    if prev is not None:
        delta = b.total - prev.total
        pct = f" ({delta * 100 / prev.total:+.1f}%)" if prev.total else ""
        rows.append([f"Change vs {period_of(prev.cycle)[0]:%b %Y}", f"{money(delta, cur, signed=True)}{pct}"])
    else:
        rows.append(["vs previous month", "first invoice"])
    rows = [[safe(c) if isinstance(c, str) else c for c in r] for r in rows]
    summary = Table(rows, colWidths=[CONTENT_W * 0.24, CONTENT_W * 0.22])
    summary.setStyle(_table_style([
        ("BACKGROUND", (0, 0), (-1, -1), MAGENTA_TINT),
        ("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 1), (1, 1)), ("SPAN", (0, 2), (1, 2)),
        ("FONTNAME", (0, 3), (-1, -1), REGULAR), ("FONTSIZE", (0, 3), (-1, -1), 8.8),
        ("TEXTCOLOR", (0, 3), (-1, -1), INK), ("ALIGN", (1, 3), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, 3), (-1, 3), 0.6, colors.HexColor("#F3B6D3")),
        ("FONTNAME", (0, 5), (-1, 5), BOLD), ("FONTNAME", (0, 7), (-1, 7), BOLD),
        ("LINEABOVE", (0, 5), (-1, 5), 0.4, colors.HexColor("#F3B6D3")),
        ("LINEABOVE", (0, 8), (-1, 8), 0.4, colors.HexColor("#F3B6D3")),
        ("TEXTCOLOR", (0, 8), (-1, 8), MUTED),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, 0), 12), ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, 1), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -2), 3),
    ]))
    outer = Table([[cust, summary]], colWidths=[CONTENT_W * 0.54, CONTENT_W * 0.46])
    outer.setStyle(_table_style([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return outer


UNIT_LABELS = {"pcs": "", "sms": "SMS", "mms": "MMS"}


def _qty(line: BillLine | None) -> str:
    if line is None or line.unit in ("total", "adjustment"):
        return ""
    return f"{line.quantity:,} {UNIT_LABELS.get(line.unit, line.unit)}".strip()


def _unit_price(line: BillLine, cur: str) -> str:
    if line.code.startswith("PLAN_FEE:"):  # pro-rated: show the full monthly fee it was pro-rated from
        try:
            return f"{money(get_plan(line.code.split(':', 2)[2]).monthly_fee, cur)}/month"
        except (KeyError, IndexError):
            pass
    if line.quantity > 1 and line.unit not in ("total", "adjustment"):
        unit = {"pcs": "each", "days": "day"}.get(line.unit, UNIT_LABELS.get(line.unit, line.unit))
        return f"{rate_str(Decimal(line.amount) / line.quantity, cur)}/{unit}"
    return ""


def _line_sort_key(line: BillLine) -> tuple:
    if line.category == LineCategory.ROAMING:  # group roaming by zone: data, calls, incoming per zone
        zone = line.code.split(":", 1)[1] if ":" in line.code else ""
        return (zone, line.code.split(":", 1)[0])
    return ()


def _charges(ctx: InvoiceContext, st) -> list:
    b, cur = ctx.bill, ctx.bill.currency
    head = ["Description", "Quantity", "Unit price", "Amount (net)"]
    widths = [CONTENT_W * w for w in (0.49, 0.15, 0.19, 0.17)]
    data: list[list] = [head]
    style = [
        ("FONTNAME", (0, 0), (-1, 0), BOLD), ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 1), (-1, -1), 8.8), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    present = [c for c in LineCategory if any(line.category == c for line in b.lines)]
    for cat in present:
        lines = sorted((x for x in b.lines if x.category == cat), key=_line_sort_key)
        r = len(data)
        data.append([Paragraph(f"<b>{_x(CATEGORY_TITLES.get(cat, cat.value.title()))}</b>", st["cell"]), "", "", ""])
        style += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), PANEL), ("TOPPADDING", (0, r), (-1, r), 5)]
        for line in lines:
            data.append([Paragraph(_x(line.label), st["cell"]), safe(_qty(line)), safe(_unit_price(line, cur)),
                         safe(money(line.amount, cur))])
            style.append(("LINEBELOW", (0, len(data) - 1), (-1, len(data) - 1), 0.25, RULE))
        sub = sum(x.amount for x in lines)
        r = len(data)
        data.append([Paragraph(f"Subtotal {_x(CATEGORY_TITLES.get(cat, cat.value).lower())}",
                               ParagraphStyle("st", parent=st["cell"], textColor=MUTED)), "", "", safe(money(sub, cur))])
        style += [("FONTNAME", (3, r), (3, r), BOLD), ("BOTTOMPADDING", (0, r), (-1, r), 8)]
    if not present:
        data.append([Paragraph("No charges in this billing period.", st["cell"]), "", "", safe(money(0, cur))])
    vat_pct = f"{float(b.vat_rate) * 100:g}%"
    r0 = len(data)
    data += [["", "", "Subtotal (net)", safe(money(b.subtotal, cur))],
             ["", "", f"VAT {vat_pct}", safe(money(b.vat, cur))],
             ["", "", "Total incl. VAT", safe(money(b.total, cur))]]
    style += [("LINEABOVE", (2, r0), (-1, r0), 0.8, INK), ("TOPPADDING", (0, r0), (-1, r0), 7),
              ("FONTNAME", (2, -1), (-1, -1), BOLD), ("FONTSIZE", (2, -1), (-1, -1), 10),
              ("LINEABOVE", (2, -1), (-1, -1), 0.4, RULE), ("TEXTCOLOR", (2, -1), (-1, -1), MAGENTA)]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(_table_style(style))
    return [_h2("Itemised charges", st), t]


def _usage(ctx: InvoiceContext, st) -> list:
    u, plan = ctx.bill.usage, ctx.plan

    def allow(v: int | None, unit: str) -> str:
        return "Unlimited" if v is None else f"{v:,} {unit}"

    rows: list[list[str]] = [["Service", "Used", "Included", "Notes"]]
    data_note = f"{u.data_mb * 100 / u.data_allowance_mb:.0f}% of allowance" if u.data_allowance_mb else ""
    rows.append(["Mobile data (incl. EU roaming)", fmt_mb(u.data_mb), fmt_mb(u.data_allowance_mb), data_note])
    if u.eu_roaming_data_mb:
        rows.append(["  of which EU roaming", fmt_mb(u.eu_roaming_data_mb), "", "Roam like at home"])
    if u.zero_rated_mb:
        rows.append(["Zero-rated data", fmt_mb(u.zero_rated_mb), "", "Not counted"])
    for zone, mb in sorted(u.roaming_data_mb.items()):
        rows.append([f"Roaming data, zone {zone}", fmt_mb(mb), "-", "Charged per MB"])
    rows.append(["Calls (domestic + EU)", f"{u.voice_min:,} min",
                 allow(plan.voice_min if plan else None, "min"), ""])
    if u.incoming_min:
        rows.append(["Incoming calls", f"{u.incoming_min:,} min", "", "Free at home"])
    for zone, m in sorted(u.roaming_voice_min.items()):
        rows.append([f"Roaming calls, zone {zone}", f"{m:,} min", "-", "Charged per minute"])
    for zone, m in sorted(u.roaming_incoming_min.items()):
        if m:
            rows.append([f"Incoming calls while roaming, zone {zone}", f"{m:,} min", "-",
                         "Free (EU)" if zone == "EU" else "Charged per minute"])
    if u.intl_voice_min:
        rows.append(["International calls", f"{u.intl_voice_min:,} min", "-", "Charged per minute"])
    if u.free_calls_min:
        rows.append(["Emergency and toll-free calls", f"{u.free_calls_min:,} min", "", "Always free"])
    rows.append(["SMS", f"{u.sms:,}", allow(plan.sms if plan else None, "SMS"), ""])
    if u.mms:
        rows.append(["MMS", f"{u.mms:,}", "-", "Charged per message"])
    if u.intl_sms:
        rows.append(["International SMS", f"{u.intl_sms:,}", "-", "Charged per message"])
    if u.third_party_charges:
        rows.append(["Third-party services", money(u.third_party_charges, ctx.bill.currency), "-",
                     "Paid to providers"])
    rows = [[safe(c) for c in r] for r in rows]
    # Used / Included wide enough for "30,000 MB (30.00 GB)" (it overlapped the next column at 0.21/0.19)
    t = Table(rows, colWidths=[CONTENT_W * w for w in (0.30, 0.245, 0.245, 0.21)], repeatRows=1)
    t.setStyle(_table_style([
        ("FONTNAME", (0, 0), (-1, 0), BOLD), ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.8), ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("TEXTCOLOR", (3, 1), (3, -1), MUTED), ("ALIGN", (1, 0), (2, -1), "RIGHT"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (3, 0), (3, -1), 14),
    ]))
    return [_h2("Usage summary", st), t]


def _explanation(ctx: InvoiceContext, st) -> list:
    b, prev, cur = ctx.bill, ctx.previous_bill, ctx.bill.currency
    ex = explain(b, prev)
    lines_now = {x.code: x for x in b.lines}
    lines_prev = {x.code: x for x in prev.lines} if prev else {}

    def q(line: BillLine | None) -> str:
        return (_qty(line) or money(line.amount, cur)) if line else "none"

    bullets: list[str] = []
    if ex.direction == "FIRST_BILL":
        bullets.append(f"This is your first invoice: {money(b.total, cur)} including VAT.")
    elif ex.direction == "SAME":
        bullets.append(f"Your bill is unchanged at {money(b.total, cur)} including VAT.")
    else:
        word = "increased" if ex.direction == "UP" else "decreased"
        pct = f" ({abs(ex.delta_pct):.1f}%)" if ex.delta_pct is not None else ""
        bullets.append(f"Your bill {word} by {money(abs(ex.delta_total or 0), cur)}{pct}, from "
                       f"{money(ex.previous_total or 0, cur)} to {money(b.total, cur)} including VAT.")
    for d in ex.drivers[:3]:
        bullets.append(f"{d.label}: {q(lines_now.get(d.code))} this period vs {q(lines_prev.get(d.code))} last period "
                       f"({money(d.delta, cur, signed=True)} before VAT).")
    shown = {d.code for d in ex.drivers[:3]}
    for t in ex.top_usage_drivers:
        if t.code in shown:
            continue
        bullets.append(f"{t.label}: {money(t.amount, cur)} ({t.share_pct:g}% of your charges beyond the plan fee).")
    if not ex.top_usage_drivers:
        bullets.append("No charges beyond your plan fee this period.")
    bullets += ex.usage_changes
    paras = [Paragraph(_x(s), st["bullet"], bulletText="•") for s in bullets]
    note = Paragraph("Computed directly from your rated charges and usage; no estimates.", st["small"])
    return [KeepTogether([_h2("Why this bill changed", st), *paras, note])]


def _payments(ctx: InvoiceContext, st) -> list:
    cur = ctx.bill.currency
    out: list = [_h2("Payments", st)]
    entries = list(ctx.payments)
    recorded = sum(p.amount for p in entries)
    if ctx.amount_paid > recorded:  # settled outside the payment ledger (direct debit import, advance credit)
        entries.append(PaymentEntry(ctx.amount_paid - recorded, None, "Direct debit / account credit", ""))
    if not entries:
        out.append(Paragraph("No payments yet.", st["base"]))
        return out

    def ref(r: str) -> str:
        return r if len(r) <= 18 else r[:16] + "…"

    rows = [["Date", "Method", "Reference", "Amount"]]
    rows += [[fmt_date(p.paid_at), p.method or "-", ref(p.reference) or "-", money(p.amount, cur)] for p in entries]
    rows.append(["", "", "Total paid", money(ctx.amount_paid, cur)])
    rows = [[safe(c) for c in r] for r in rows]
    t = Table(rows, colWidths=[CONTENT_W * w for w in (0.18, 0.34, 0.3, 0.18)])
    t.setStyle(_table_style([
        ("FONTNAME", (0, 0), (-1, 0), BOLD), ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("FONTSIZE", (0, 1), (-1, -1), 8.8), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE), ("FONTNAME", (2, -1), (-1, -1), BOLD),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    out.append(t)
    return out


def _how_to_pay(ctx: InvoiceContext, st) -> list:
    cur = ctx.bill.currency
    if ctx.outstanding:
        text = (f"Please pay <b>{_x(money(ctx.outstanding, cur))}</b> by <b>{_x(fmt_date(ctx.due_date))}</b>. "
                "In the Self-care app, open this bill under <b>Bills</b> and tap <b>Pay</b>; the payment is applied "
                f"to this invoice ({_x(ctx.invoice_number)}) automatically and usually shows within a few minutes.")
    elif ctx.bill.total <= 0:
        text = "There is nothing to pay for this invoice. No action is needed."
    else:
        text = "This invoice is paid in full. No action is needed."
    body = [Paragraph("<b>How to pay</b>", st["base"]), Spacer(1, 3), Paragraph(text, st["base"]), Spacer(1, 4),
            Paragraph("Questions about a charge? Open this bill in the Self-care app: it explains every change and "
                      "can add up the usage records behind it.", st["small"])]
    box = Table([[body]], colWidths=[CONTENT_W])
    box.setStyle(_table_style([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("LINEBEFORE", (0, 0), (0, -1), 3, MAGENTA),
                             ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                             ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    return [Spacer(1, 16), KeepTogether([box])]


# ---------------------------------------------------------------- page furniture

def _canvas_factory(ctx: InvoiceContext):
    invoice_no, currency = safe(ctx.invoice_number), ctx.bill.currency

    class NumberedCanvas(rl_canvas.Canvas):
        """Two-pass page numbering: collect page states, then draw 'Page X of Y' on each at save()."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("initialFontName", REGULAR)   # the canvas default would be Helvetica
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._furniture(total)
                super().showPage()
            super().save()

        def _furniture(self, total: int) -> None:
            self.saveState()
            y = 14 * mm
            self.setStrokeColor(RULE)
            self.setLineWidth(0.5)
            self.line(MARGIN_X, y + 9, PAGE_W - MARGIN_X, y + 9)
            self.setFont(REGULAR, 7.5)
            self.setFillColor(MUTED)
            self.drawString(MARGIN_X, y, f"Self-care  ·  Invoice {invoice_no}")
            self.drawRightString(PAGE_W - MARGIN_X, y, f"Page {self._pageNumber} of {total}")
            self.drawString(MARGIN_X, y - 10, f"Amounts in {currency}; VAT included as shown. Charges are computed "
                                              "from network usage records (CDRs) rated against your tariff.")
            if self._pageNumber > 1:
                self.setFont(BOLD, 9)
                self.setFillColor(MAGENTA)
                self.drawString(MARGIN_X, PAGE_H - 12 * mm, "Self-care")
                self.setFont(REGULAR, 8)
                self.setFillColor(MUTED)
                self.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 12 * mm, f"Invoice {invoice_no} (continued)")
            self.restoreState()

    return NumberedCanvas


def render_bill_pdf(ctx: InvoiceContext, *, compress: bool = True) -> bytes:
    """Render the invoice. `compress=False` leaves content streams readable (used by tests)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=16 * mm, bottomMargin=26 * mm,
        title=safe(f"Invoice {ctx.invoice_number}"), author="Self-care", subject=f"Mobile invoice {ctx.bill.cycle}",
        creator="Self-care", invariant=1, pageCompression=1 if compress else 0)
    st = _styles()
    story: list = []
    story += _header(ctx, st)
    story.append(_customer_and_summary(ctx, st))
    story += _charges(ctx, st)
    story += _usage(ctx, st)
    story += _explanation(ctx, st)
    story += _payments(ctx, st)
    story += _how_to_pay(ctx, st)
    doc.build(story, canvasmaker=_canvas_factory(ctx))
    return buf.getvalue()
