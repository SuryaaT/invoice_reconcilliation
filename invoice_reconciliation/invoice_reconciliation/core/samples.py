"""SYNTHETIC / FICTIONAL demo data: 12 scenarios. No real companies, GSTINs or people."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D

from .models import GRN, GRNLine, Invoice, InvoiceLine, PO, POLine
from .reconcile import q2

BUYER = ("Chennai Textile Mills Pvt Ltd (DEMO)", "33AAACC3456D1Z8")
SUP = {"A": ("Sri Murugan Industrial Supplies (DEMO)", "33AABCS1234F1ZP"),    # Tamil Nadu -> intra-state
       "B": ("Bengaluru Packaging Co (DEMO)", "29AAGCB5678K1Z2"),             # Karnataka  -> inter-state (IGST)
       "C": ("Coimbatore Fasteners Pvt Ltd (DEMO)", "33AAJCC9012M1ZX")}       # Tamil Nadu -> intra-state
ITEMS = {  # key: (description, HSN, unit, PO price, GST %)
    "paper": ("A4 Copier Paper 75 GSM", "4802", "REAM", D("285.00"), D("12")),
    "bolt50": ("Steel Bolt M10 x 50mm", "7318", "NOS", D("4.50"), D("18")),
    "bolt80": ("Steel Bolt M10 x 80mm", "7318", "NOS", D("6.10"), D("18")),
    "box": ("Corrugated Box 5-Ply 18x12x10 in", "4819", "NOS", D("38.00"), D("12")),
    "tape": ("BOPP Packing Tape 48mm x 100m", "3919", "ROLL", D("52.00"), D("18")),
    "oil": ("Machine Lubricant Oil 5L Can", "2710", "CAN", D("940.00"), D("18")),
    "gloves": ("Cotton Safety Gloves", "6116", "PAIR", D("22.50"), D("12")),
}


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    expected: str
    po: PO
    grn: GRN | None
    invoice: Invoice
    requires: tuple = ()


def _po(n, sup, items, freight="0"):
    name, g = SUP[sup]
    lines = [POLine(line_ref=str(i), description=ITEMS[k][0], hsn=ITEMS[k][1], qty=D(q), unit=ITEMS[k][2],
                    unit_price=ITEMS[k][3], gst_rate=ITEMS[k][4]) for i, (k, q) in enumerate(items, 1)]
    return PO(po_number=f"PO-{n}", po_date=date(2026, 8, 20), supplier_name=name, supplier_gstin=g, currency="INR",
              authorized_freight=D(freight), lines=lines)


def _grn(n, po, accepted):
    return GRN(grn_number=f"GRN-{n}", po_number=po.po_number, grn_date=date(2026, 8, 28),
               lines=[GRNLine(po_line_ref=l.line_ref, description=l.description, hsn=l.hsn, received_qty=D(a),
                              accepted_qty=D(a), unit=l.unit) for l, a in zip(po.lines, accepted)])


def _sum(inv: Invoice) -> Invoice:
    inv.subtotal = sum((l.taxable_value for l in inv.lines), D(0))
    inv.cgst_total = sum((l.cgst for l in inv.lines), D(0))
    inv.sgst_total = sum((l.sgst for l in inv.lines), D(0))
    inv.igst_total = sum((l.igst for l in inv.lines), D(0))
    for l in inv.lines:
        l.line_total = l.taxable_value + l.cgst + l.sgst + l.igst
    inv.grand_total = inv.subtotal + inv.cgst_total + inv.sgst_total + inv.igst_total + inv.freight + inv.other_charges + inv.round_off
    return inv


def _inv(no, dt, sup, po_no, items, freight="0"):
    """items: (key, qty, price|None, description override|None). Tax is computed correctly; scenarios then break it."""
    name, g = SUP[sup]
    inter = g[:2] != BUYER[1][:2]
    lines = []
    for i, (k, q, p, desc) in enumerate(items, 1):
        d, h, u, pr, rate = ITEMS[k]
        price = D(p) if p else pr
        taxable = q2(D(q) * price)
        tax = q2(taxable * rate / (100 if inter else 200))
        lines.append(InvoiceLine(line_no=i, description=desc or d, hsn=h, qty=D(q), unit=u, unit_price=price,
                                 taxable_value=taxable, gst_rate=rate, cgst=D(0) if inter else tax,
                                 sgst=D(0) if inter else tax, igst=tax if inter else D(0)))
    inv = Invoice(invoice_no=no, invoice_date=date.fromisoformat(dt), supplier_name=name, supplier_gstin=g,
                  buyer_gstin=BUYER[1], po_number=po_no, currency="INR", lines=lines, subtotal=0, grand_total=0,
                  freight=D(freight))
    return _sum(inv)


def scenarios() -> list[Scenario]:
    out = []

    def add(*a, **k):
        out.append(Scenario(*a, **k))

    po1 = _po(1, "A", [("paper", 100), ("bolt50", 500)])
    grn1 = _grn(1, po1, [100, 500])
    l1 = [("paper", 100, None, None), ("bolt50", 500, None, None)]
    add("S01", "Perfect match", "PO, GRN and invoice all agree.", "RECOMMENDED_FOR_APPROVAL", po1, grn1,
        _inv("SMI/2026/0901", "2026-09-01", "A", "PO-1", l1))

    po2 = _po(2, "A", [("paper", 100)])
    add("S02", "Invoice qty exceeds PO qty", "Invoiced 120 reams; ordered and received 100.", "REQUIRES_REVIEW", po2,
        _grn(2, po2, [100]), _inv("SM-INV-2218", "2026-09-06", "A", "PO-2", [("paper", 120, None, None)]))

    po3 = _po(3, "C", [("box", 100)])
    add("S03", "Invoice qty exceeds received qty (partial delivery)",
        "100 boxes invoiced but only 70 received: 30 flagged as not yet received.", "REQUIRES_REVIEW", po3,
        _grn(3, po3, [70]), _inv("CF-7731", "2026-09-11", "C", "PO-3", [("box", 100, None, None)]))

    po4 = _po(4, "A", [("bolt50", 1000)])
    add("S04", "Unit price higher than PO", "Bolt billed at 4.80 vs PO 4.50 (6.7% over).", "REQUIRES_REVIEW", po4,
        _grn(4, po4, [1000]), _inv("SM-INV-2240", "2026-09-16", "A", "PO-4", [("bolt50", 1000, "4.80", None)]))

    po5 = _po(5, "C", [("oil", 10)])
    inv5 = _inv("CF-7790", "2026-09-21", "C", "PO-5", [("oil", 10, None, None)])
    inv5.lines[0].cgst = inv5.lines[0].sgst = q2(inv5.lines[0].taxable_value * D(6) / 100)  # 6% each instead of 9%
    add("S05", "Incorrect GST calculation", "18% GST declared but CGST/SGST computed at 6% each.", "REQUIRES_REVIEW", po5,
        _grn(5, po5, [10]), _sum(inv5))

    po6 = _po(6, "C", [("gloves", 400)])
    inv6 = _inv("CF-7802", "2026-09-26", "C", "PO-6", [("gloves", 400, None, None)])
    inv6.grand_total += D("250.00")
    add("S06", "Invoice total arithmetic error", "Grand total overstated by Rs 250.", "REQUIRES_REVIEW", po6,
        _grn(6, po6, [400]), inv6)

    add("S07", "Exact duplicate invoice", "Same supplier GSTIN and invoice number as S01 (different formatting).",
        "REJECTED_BY_RULES", po1, grn1, _inv("SMI-2026-0901", "2026-09-01", "A", "PO-1", l1), requires=("S01",))

    po8 = _po(8, "B", [("tape", 200)])
    add("S08", "Missing GRN", "PO exists but goods were never receipted.", "PENDING_DOCUMENTS", po8, None,
        _inv("BP/26-27/118", "2026-10-01", "B", "PO-8", [("tape", 200, None, None)]))

    po9 = _po(9, "C", [("bolt50", 200), ("bolt80", 200)])
    add("S09", "Ambiguous product description",
        "Invoice says just 'Steel Bolt M10' - could be the 50mm or 80mm PO line. Resolve in Human Review.",
        "REQUIRES_REVIEW", po9, _grn(9, po9, [200, 200]),
        _inv("CF-7955", "2026-10-06", "C", "PO-9", [("bolt80", 200, None, "Steel Bolt M10")]))

    po10 = _po(10, "B", [("oil", 10)])
    add("S10", "Unauthorized freight charge", "Rs 1,500 freight billed; PO authorizes none.", "REQUIRES_REVIEW", po10,
        _grn(10, po10, [10]), _inv("BP/26-27/131", "2026-10-11", "B", "PO-10", [("oil", 10, None, None)], freight="1500"))

    add("S11", "Potential duplicate invoice", "Different invoice number ('...0901R') but same supplier, amount and date +1.",
        "REQUIRES_REVIEW", po1, grn1, _inv("SMI/2026/0901R", "2026-09-02", "A", "PO-1", l1), requires=("S01",))

    po12 = _po(12, "C", [("box", 100)])
    add("S12", "Partial delivery billed correctly", "70 of 100 received; invoice is for 70 only - not rejected.",
        "RECOMMENDED_FOR_APPROVAL", po12, _grn(12, po12, [70]),
        _inv("CF-7931", "2026-10-16", "C", "PO-12", [("box", 70, None, None)]))
    return out


# ---------- render documents as text/PDF in the layout the demo fallback parser understands ----------
def _c(v):
    return "-" if v is None else str(v)


def render_text(kind: str, m) -> str:
    L = ["SYNTHETIC DEMO DOCUMENT - FICTIONAL DATA"]
    if kind == "invoice":
        L += ["TAX INVOICE", f"Invoice No: {m.invoice_no}", f"Invoice Date: {m.invoice_date}", f"Supplier: {m.supplier_name}",
              f"Supplier GSTIN: {m.supplier_gstin}", f"Buyer GSTIN: {_c(m.buyer_gstin)}", f"PO Number: {_c(m.po_number)}",
              f"Currency: {m.currency}", "# | PO Ref | Description | HSN | Qty | Unit | Unit Price | Taxable | GST % | CGST | SGST | IGST | Line Total"]
        L += [" | ".join(_c(x) for x in (l.line_no, l.po_line_ref, l.description, l.hsn, l.qty, l.unit, l.unit_price,
              l.taxable_value, l.gst_rate, l.cgst, l.sgst, l.igst, l.line_total)) for l in m.lines]
        L += [f"Subtotal: {m.subtotal}", f"CGST Total: {m.cgst_total}", f"SGST Total: {m.sgst_total}",
              f"IGST Total: {m.igst_total}", f"Freight: {m.freight}", f"Other Charges: {m.other_charges}",
              f"Round Off: {m.round_off}", f"Grand Total: {m.grand_total}"]
    elif kind == "po":
        L += ["PURCHASE ORDER", f"PO Number: {m.po_number}", f"PO Date: {_c(m.po_date)}", f"Supplier: {m.supplier_name}",
              f"Supplier GSTIN: {m.supplier_gstin}", f"Currency: {m.currency}", f"Authorized Freight: {m.authorized_freight}",
              "# | Ref | Description | HSN | Qty | Unit | Unit Price | GST %"]
        L += [" | ".join(_c(x) for x in (l.line_ref, l.line_ref, l.description, l.hsn, l.qty, l.unit, l.unit_price, l.gst_rate))
              for l in m.lines]
    else:
        L += ["GOODS RECEIPT NOTE", f"GRN Number: {m.grn_number}", f"PO Number: {m.po_number}", f"GRN Date: {_c(m.grn_date)}",
              "# | PO Line Ref | Description | HSN | Received | Accepted | Unit"]
        L += [" | ".join(_c(x) for x in (i, l.po_line_ref, l.description, l.hsn, l.received_qty, l.accepted_qty, l.unit))
              for i, l in enumerate(m.lines, 1)]
    return "\n".join(L)


def render_pdf(text: str) -> bytes:
    import fitz  # PyMuPDF
    doc = fitz.open()
    lines = text.splitlines()
    for start in range(0, len(lines), 42):
        page = doc.new_page(width=842, height=595)
        for i, ln in enumerate(lines[start:start + 42]):
            page.insert_text((30, 40 + i * 12), ln, fontsize=7.5, fontname="helv")
    return doc.tobytes()
