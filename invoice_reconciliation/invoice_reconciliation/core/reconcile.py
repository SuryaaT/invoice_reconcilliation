"""Deterministic three-way reconciliation engine (PO <-> GRN <-> Invoice).
Pure functions, Decimal only. The LLM never touches anything in this file."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher

from config import Tolerances
from .matching import match_line
from .models import (Candidate, CheckRow, ExceptionItem, GRNLine, Invoice, LineResult, PO,
                     ReconResult, TaxRow, TotalRow)

D0 = Decimal("0")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
_UNITS = {"NOS": "NOS", "NO": "NOS", "PCS": "NOS", "PC": "NOS", "EA": "NOS", "EACH": "NOS", "UNIT": "NOS",
          "UNITS": "NOS", "REAM": "REAM", "REAMS": "REAM", "BOX": "BOX", "BOXES": "BOX", "KG": "KG",
          "KGS": "KG", "L": "L", "LTR": "L", "LITRE": "L", "LITRES": "L", "PAIR": "PAIR", "PAIRS": "PAIR",
          "ROLL": "ROLL", "ROLLS": "ROLL", "CAN": "CAN", "CANS": "CAN"}


def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm_gstin(g) -> str:
    return re.sub(r"\s+", "", (g or "").upper())


def norm_inv_no(n) -> str:
    return re.sub(r"[^A-Z0-9]", "", (n or "").upper())


def norm_unit(u) -> str:
    u = re.sub(r"[^A-Z]", "", (u or "").upper())
    return _UNITS.get(u, u)


EXPLAIN = {
    "REJECTED_BY_RULES": "Hard-stop rule triggered (exact duplicate). The system will not recommend this invoice.",
    "PENDING_DOCUMENTS": "A required document (PO or GRN) is missing, so three-way matching is incomplete. Do not approve until it is provided.",
    "REQUIRES_REVIEW": "One or more discrepancies need a human decision. See the exceptions list.",
    "RECOMMENDED_FOR_APPROVAL": "All automated checks passed within tolerance. A human must still approve; no payment is initiated.",
}


def reconcile(inv: Invoice, po: PO | None, grn_lines: list[GRNLine] | None, tol: Tolerances,
              prior_invoices=(), overrides: dict[int, str] | None = None) -> ReconResult:
    overrides = overrides or {}
    ex: list[ExceptionItem] = []
    checks: list[CheckRow] = []

    def add(code, sev, cat, desc, line=None, exp=None, act=None):
        diff = str(act - exp) if isinstance(exp, Decimal) and isinstance(act, Decimal) else None
        ex.append(ExceptionItem(code=code, severity=sev, category=cat, line_no=line, description=desc,
                                expected=None if exp is None else str(exp),
                                actual=None if act is None else str(act), difference=diff))

    # ---- E. duplicates ----
    key = norm_gstin(inv.supplier_gstin) or inv.supplier_name.upper()
    exact = False
    for p in prior_invoices:
        pkey = norm_gstin(p["supplier_gstin"]) or p["supplier_name"].upper()
        if pkey == key and norm_inv_no(p["invoice_no"]) == norm_inv_no(inv.invoice_no):
            add("DUPLICATE_EXACT", "CRITICAL", "Duplicate",
                f"Same supplier GSTIN and normalized invoice number as existing invoice '{p['invoice_no']}' "
                f"(record {p['id'][:8]}). Hard stop.")
            exact = True
            break
    if not exact:
        for p in prior_invoices:
            if (norm_gstin(p["supplier_gstin"]) or p["supplier_name"].upper()) != key:
                continue
            sim = SequenceMatcher(None, norm_inv_no(p["invoice_no"]), norm_inv_no(inv.invoice_no)).ratio()
            pamt = Decimal(p["grand_total"])
            close = abs(pamt - inv.grand_total) <= tol.dup_amount_pct / 100 * max(pamt, inv.grand_total)
            days = abs((date.fromisoformat(p["invoice_date"]) - inv.invoice_date).days)
            hits = [sim >= tol.dup_number_similarity, close, days <= tol.dup_days]
            if sum(hits) >= 2:
                add("DUPLICATE_POTENTIAL", "HIGH", "Duplicate",
                    f"Looks similar to existing invoice '{p['invoice_no']}' from the same supplier "
                    f"(number similarity {sim:.0%}, amount {'close' if close else 'different'}, "
                    f"dates {days} day(s) apart). Needs human confirmation.")
                break

    # ---- F. missing documents ----
    if po is None:
        add("MISSING_PO", "HIGH", "Missing documents",
            f"No purchase order on file for reference '{inv.po_number or 'none given on invoice'}'.")
    elif not grn_lines:
        add("MISSING_GRN", "HIGH", "Missing documents", f"No goods receipt note on file for {po.po_number}.")
    if po is not None:
        checks.append(CheckRow(check="GRN on file", expected="Yes", actual="Yes" if grn_lines else "No",
                               ok=bool(grn_lines)))

    # ---- header checks ----
    if not GSTIN_RE.match(norm_gstin(inv.supplier_gstin)):
        add("GSTIN_FORMAT", "MEDIUM", "Data quality",
            "Supplier GSTIN is not in the 15-character GSTIN format (format check only; not verified against the GST portal).",
            act=None)
    if po is not None:
        ok = norm_gstin(po.supplier_gstin) == norm_gstin(inv.supplier_gstin)
        checks.append(CheckRow(check="Supplier GSTIN", expected=po.supplier_gstin, actual=inv.supplier_gstin, ok=ok))
        if not ok:
            add("SUPPLIER_MISMATCH", "HIGH", "Supplier", "Invoice supplier GSTIN differs from the PO supplier.")
        ok = po.currency.upper() == inv.currency.upper()
        checks.append(CheckRow(check="Currency", expected=po.currency, actual=inv.currency, ok=ok))
        if not ok:
            add("CURRENCY_MISMATCH", "HIGH", "Currency", "Invoice currency differs from PO currency.")
        charges = inv.freight + inv.other_charges
        ok = charges <= po.authorized_freight
        checks.append(CheckRow(check="Freight/other charges", expected=f"<= {po.authorized_freight}",
                               actual=str(charges), ok=ok))
        if not ok:
            add("UNAUTHORIZED_CHARGES", "HIGH", "Charges",
                "Freight/other charges exceed what the PO authorizes.", exp=po.authorized_freight, act=charges)

    # ---- A/B/G. line matching, quantity, price ----
    grn_acc = defaultdict(lambda: [D0, D0])
    for g in grn_lines or []:
        grn_acc[g.po_line_ref][0] += g.received_qty
        grn_acc[g.po_line_ref][1] += g.accepted_qty

    lines: list[LineResult] = []
    for l in inv.lines:
        lr = LineResult(line_no=l.line_no, description=l.description, inv_qty=l.qty, inv_price=l.unit_price)
        lines.append(lr)
        if po is None:
            continue
        status, pl, cands, sc = match_line(l, po.lines, overrides.get(l.line_no))
        lr.match_status = status
        lr.candidates = [Candidate(line_ref=p.line_ref, description=p.description, score=round(s, 2))
                         for s, p in cands]
        if status == "NO_MATCH":
            add("PRODUCT_MISMATCH", "HIGH", "Product matching",
                f"Line {l.line_no} '{l.description}' does not match any PO line.", line=l.line_no)
            continue
        if status == "AMBIGUOUS":
            names = "; ".join(f"[{p.line_ref}] {p.description}" for _, p in cands)
            add("AMBIGUOUS_MATCH", "MEDIUM", "Product matching",
                f"Line {l.line_no} '{l.description}' could be several PO lines: {names}. Reviewer must choose.",
                line=l.line_no)
            continue
        lr.po_line_ref, lr.po_qty, lr.po_price = pl.line_ref, pl.qty, pl.unit_price
        if status == "MATCHED" and sc < 0.3:
            add("PRODUCT_MISMATCH", "MEDIUM", "Product matching",
                f"Line {l.line_no} matched PO line {pl.line_ref} by reference but descriptions differ substantially.",
                line=l.line_no)
        if norm_unit(l.unit) != norm_unit(pl.unit):
            add("UOM_MISMATCH", "MEDIUM", "Unit of measure",
                f"Line {l.line_no}: invoice unit '{l.unit}' vs PO unit '{pl.unit}'. Quantity/price not compared.",
                line=l.line_no)
            continue
        if l.qty > pl.qty:
            add("QTY_EXCEEDS_PO", "HIGH", "Quantity",
                f"Line {l.line_no}: invoiced quantity exceeds ordered quantity.", line=l.line_no,
                exp=pl.qty, act=l.qty)
        if grn_lines:
            lr.grn_accepted = grn_acc[pl.line_ref][1]
            lr.not_received = max(D0, l.qty - lr.grn_accepted)
            if l.qty > lr.grn_accepted:
                add("QTY_NOT_RECEIVED", "HIGH", "Quantity",
                    f"Line {l.line_no}: {lr.not_received} {l.unit} invoiced but not yet received/accepted "
                    f"(partial delivery). Not an automatic rejection.", line=l.line_no,
                    exp=lr.grn_accepted, act=l.qty)
            elif l.qty < lr.grn_accepted:
                add("QTY_BELOW_RECEIVED", "INFO", "Quantity",
                    f"Line {l.line_no}: invoice covers less than was received; balance may be billed later.",
                    line=l.line_no, exp=lr.grn_accepted, act=l.qty)
        diff = l.unit_price - pl.unit_price
        pct = (abs(diff) / pl.unit_price * 100) if pl.unit_price else (D0 if diff == 0 else Decimal(100))
        lr.price_var_pct = (diff / pl.unit_price * 100).quantize(Decimal("0.01")) if pl.unit_price else None
        if pct > tol.price_pct:
            add("PRICE_MISMATCH", "HIGH" if diff > 0 else "MEDIUM", "Price",
                f"Line {l.line_no}: unit price differs from PO by {pct.quantize(Decimal('0.01'))}% "
                f"(tolerance {tol.price_pct}%).", line=l.line_no, exp=pl.unit_price, act=l.unit_price)
        if pl.gst_rate is not None and l.gst_rate is not None and pl.gst_rate != l.gst_rate:
            add("TAX_RATE_MISMATCH", "HIGH", "Tax", f"Line {l.line_no}: GST rate differs from PO.",
                line=l.line_no, exp=pl.gst_rate, act=l.gst_rate)

    # ---- D. line arithmetic (separate from tax) ----
    for l in inv.lines:
        calc = q2(l.qty * l.unit_price)
        if l.taxable_value is not None and abs(l.taxable_value - calc) > tol.line_abs:
            add("LINE_MATH_ERROR", "MEDIUM", "Line arithmetic",
                f"Line {l.line_no}: qty x unit price does not equal declared taxable value.",
                line=l.line_no, exp=calc, act=l.taxable_value)
        if l.line_total is not None:
            base = l.taxable_value if l.taxable_value is not None else calc
            calc_tot = base + (l.cgst or D0) + (l.sgst or D0) + (l.igst or D0)
            if abs(l.line_total - calc_tot) > tol.line_abs:
                add("LINE_MATH_ERROR", "MEDIUM", "Line arithmetic",
                    f"Line {l.line_no}: declared line total != taxable value + declared taxes.",
                    line=l.line_no, exp=calc_tot, act=l.line_total)

    # ---- C. tax recalculation (uses declared taxable value so it stays independent of line arithmetic) ----
    sg, bg = norm_gstin(inv.supplier_gstin)[:2], norm_gstin(inv.buyer_gstin)[:2]
    if sg.isdigit() and bg.isdigit():
        inter = sg != bg
    else:
        inter = sum((l.igst or D0) for l in inv.lines) > 0
    tax_rows: list[TaxRow] = []
    for l in inv.lines:
        if l.gst_rate is None:
            add("TAX_RATE_MISSING", "MEDIUM", "Tax", f"Line {l.line_no}: GST rate missing; tax not recalculated.",
                line=l.line_no)
            continue
        base = l.taxable_value if l.taxable_value is not None else q2(l.qty * l.unit_price)
        if inter:
            exp = {"CGST": D0, "SGST": D0, "IGST": q2(base * l.gst_rate / 100)}
        else:
            half = q2(base * l.gst_rate / 200)
            exp = {"CGST": half, "SGST": half, "IGST": D0}
        decl = {"CGST": l.cgst or D0, "SGST": l.sgst or D0, "IGST": l.igst or D0}
        wrong_type = (inter and (decl["CGST"] > 0 or decl["SGST"] > 0)) or (not inter and decl["IGST"] > 0)
        for comp in ("CGST", "SGST", "IGST"):
            d = decl[comp] - exp[comp]
            ok = abs(d) <= tol.tax_abs
            tax_rows.append(TaxRow(line_no=l.line_no, component=comp, taxable=base, rate=l.gst_rate,
                                   declared=decl[comp], calculated=exp[comp], difference=d, ok=ok))
            if not ok and not wrong_type:
                add("TAX_MISMATCH", "HIGH", "Tax", f"Line {l.line_no}: declared {comp} differs from recalculation "
                    f"({l.gst_rate}% on {base}).", line=l.line_no, exp=exp[comp], act=decl[comp])
        if wrong_type:
            add("TAX_TYPE_MISMATCH", "HIGH", "Tax",
                f"Line {l.line_no}: {'inter-state supply should carry IGST' if inter else 'intra-state supply should carry CGST+SGST'} "
                f"(supplier state {sg or '?'}, buyer state {bg or '?'}).", line=l.line_no,
                exp=sum(exp.values(), D0), act=sum(decl.values(), D0))

    # ---- D. invoice totals ----
    s_tax = sum(((l.taxable_value if l.taxable_value is not None else q2(l.qty * l.unit_price))
                 for l in inv.lines), D0)
    s_c, s_s, s_i = (sum(((getattr(l, a) or D0) for l in inv.lines), D0) for a in ("cgst", "sgst", "igst"))
    grand = s_tax + s_c + s_s + s_i + inv.freight + inv.other_charges + inv.round_off
    total_rows = []
    for item, decl, calc, sev in [("Subtotal (taxable)", inv.subtotal, s_tax, "MEDIUM"), ("CGST total", inv.cgst_total, s_c, "MEDIUM"),
                                  ("SGST total", inv.sgst_total, s_s, "MEDIUM"), ("IGST total", inv.igst_total, s_i, "MEDIUM"),
                                  ("Grand total", inv.grand_total, grand, "HIGH")]:
        d = decl - calc
        ok = abs(d) <= tol.total_abs
        total_rows.append(TotalRow(item=item, declared=decl, calculated=calc, difference=d, tolerance=tol.total_abs, ok=ok))
        if not ok:
            add("TOTAL_MISMATCH", sev, "Totals", f"{item} on invoice does not match the sum of its components.",
                exp=calc, act=decl)

    # ---- outcome ----
    for lr in lines:
        lr.flags = sorted({e.code for e in ex if e.line_no == lr.line_no})
    codes = {e.code for e in ex}
    if "DUPLICATE_EXACT" in codes:
        status = "REJECTED_BY_RULES"
    elif codes & {"MISSING_PO", "MISSING_GRN"}:
        status = "PENDING_DOCUMENTS"
    elif any(e.severity != "INFO" for e in ex):
        status = "REQUIRES_REVIEW"
    else:
        status = "RECOMMENDED_FOR_APPROVAL"
    return ReconResult(status=status, explanation=EXPLAIN[status], interstate=inter, exceptions=ex, lines=lines,
                       tax_rows=tax_rows, total_rows=total_rows, doc_checks=checks)
