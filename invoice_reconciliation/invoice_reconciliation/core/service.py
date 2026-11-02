"""Persistence + workflow orchestration. All business decisions live in core.reconcile (deterministic)."""
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

import config
from config import Tolerances
from .db import audit, ins, now, uid, upd
from .models import GRN, GRNLine, Invoice, InvoiceLine, PO, POLine, ReconResult
from .reconcile import norm_gstin, norm_inv_no, reconcile
from . import samples

HDR = ["invoice_no", "invoice_date", "po_number", "buyer_gstin", "currency", "subtotal", "cgst_total", "sgst_total",
       "igst_total", "freight", "other_charges", "round_off", "grand_total"]


# ---------------- suppliers / documents ----------------
def upsert_supplier(conn, name, gstin) -> str:
    g = norm_gstin(gstin) or "UNKNOWN-" + name.upper()
    r = conn.execute("SELECT id FROM suppliers WHERE gstin=?", (g,)).fetchone()
    if r:
        return r["id"]
    sid = uid()
    ins(conn, "suppliers", id=sid, name=name, gstin=g, created_at=now())
    return sid


def save_document(conn, kind, filename, data: bytes, method=None) -> str:
    did = uid()
    config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DOCS_DIR / f"{did}_{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
    path.write_bytes(data)
    ins(conn, "documents", id=did, kind=kind, original_name=filename, stored_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), extraction_method=method, uploaded_at=now())
    audit(conn, "UPLOAD", entity_type="document", entity_id=did, kind=kind, filename=filename)
    return did


# ---------------- insert / load documents ----------------
def insert_po(conn, po: PO, doc_id=None) -> str:
    r = conn.execute("SELECT id FROM purchase_orders WHERE po_number=?", (po.po_number,)).fetchone()
    if r:
        return r["id"]
    pid = uid()
    ins(conn, "purchase_orders", id=pid, po_number=po.po_number, supplier_id=upsert_supplier(conn, po.supplier_name, po.supplier_gstin),
        po_date=po.po_date, currency=po.currency, authorized_freight=po.authorized_freight, document_id=doc_id, created_at=now())
    for l in po.lines:
        ins(conn, "po_lines", id=uid(), po_id=pid, **l.model_dump())
    audit(conn, "PO_SAVED", entity_type="purchase_order", entity_id=pid, po_number=po.po_number)
    return pid


def insert_grn(conn, grn: GRN, doc_id=None) -> str:
    r = conn.execute("SELECT id FROM grns WHERE grn_number=?", (grn.grn_number,)).fetchone()
    if r:
        return r["id"]
    gid = uid()
    ins(conn, "grns", id=gid, grn_number=grn.grn_number, po_number=grn.po_number, grn_date=grn.grn_date,
        document_id=doc_id, created_at=now())
    for l in grn.lines:
        ins(conn, "grn_lines", id=uid(), grn_id=gid, **l.model_dump())
    audit(conn, "GRN_SAVED", entity_type="grn", entity_id=gid, grn_number=grn.grn_number)
    return gid


def load_po(conn, po_number) -> PO | None:
    r = conn.execute("SELECT p.*, s.name supplier_name, s.gstin supplier_gstin FROM purchase_orders p "
                     "JOIN suppliers s ON s.id=p.supplier_id WHERE p.po_number=?", (po_number,)).fetchone()
    if not r:
        return None
    lines = [dict(x) for x in conn.execute("SELECT * FROM po_lines WHERE po_id=? ORDER BY line_ref", (r["id"],))]
    return PO.model_validate({**dict(r), "lines": lines})


def load_grn_lines(conn, po_number) -> list[GRNLine] | None:
    rows = conn.execute("SELECT l.* FROM grn_lines l JOIN grns g ON g.id=l.grn_id WHERE g.po_number=?", (po_number,)).fetchall()
    return [GRNLine.model_validate(dict(r)) for r in rows] or None


def load_invoice(conn, invoice_id) -> Invoice:
    r = conn.execute("SELECT i.*, s.name supplier_name, s.gstin supplier_gstin FROM invoices i "
                     "JOIN suppliers s ON s.id=i.supplier_id WHERE i.id=?", (invoice_id,)).fetchone()
    lines = [dict(x) for x in conn.execute("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no", (invoice_id,))]
    return Invoice.model_validate({**dict(r), "lines": lines})


def _insert_lines(conn, invoice_id, inv: Invoice):
    for l in inv.lines:
        ins(conn, "invoice_lines", id=uid(), invoice_id=invoice_id, **l.model_dump())


def insert_invoice(conn, inv: Invoice, doc_id=None, demo=None) -> str:
    iid = uid()
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM invoices").fetchone()[0]
    ins(conn, "invoices", id=iid, seq=seq, supplier_id=upsert_supplier(conn, inv.supplier_name, inv.supplier_gstin),
        norm_invoice_no=norm_inv_no(inv.invoice_no), original_json=inv.model_dump_json(), document_id=doc_id,
        demo_scenario=demo, created_at=now(), updated_at=now(), **{k: getattr(inv, k) for k in HDR})
    _insert_lines(conn, iid, inv)
    audit(conn, "INVOICE_SAVED", invoice_id=iid, entity_type="invoice", entity_id=iid, invoice_no=inv.invoice_no, demo=demo)
    return iid


# ---------------- reconciliation ----------------
def run_reconciliation(conn, invoice_id, tol: Tolerances, actor="system") -> str:
    inv = load_invoice(conn, invoice_id)
    seq = conn.execute("SELECT seq FROM invoices WHERE id=?", (invoice_id,)).fetchone()["seq"]
    po = load_po(conn, inv.po_number) if inv.po_number else None
    grn = load_grn_lines(conn, inv.po_number) if po else None
    prior = [dict(r) for r in conn.execute(
        "SELECT i.id, i.invoice_no, i.invoice_date, i.grand_total, s.gstin supplier_gstin, s.name supplier_name "
        "FROM invoices i JOIN suppliers s ON s.id=i.supplier_id WHERE i.seq < ?", (seq,))]
    ov = {r["line_no"]: r["po_line_ref"] for r in conn.execute(
        "SELECT line_no, po_line_ref FROM line_match_overrides WHERE invoice_id=?", (invoice_id,))}
    res = reconcile(inv, po, grn, tol, prior, ov)
    run_id = uid()
    ins(conn, "reconciliation_runs", id=run_id, invoice_id=invoice_id, run_at=now(), status=res.status,
        tolerances_json=json.dumps(asdict(tol), default=str), result_json=res.model_dump_json(), exception_count=len(res.exceptions))
    for e in res.exceptions:
        ins(conn, "exceptions", id=uid(), run_id=run_id, invoice_id=invoice_id, line_no=e.line_no, code=e.code,
            severity=e.severity, category=e.category, description=e.description, expected=e.expected,
            actual=e.actual, difference=e.difference)
    upd(conn, "invoices", invoice_id, status=res.status, latest_run_id=run_id, updated_at=now())
    audit(conn, "RECONCILIATION_RUN", invoice_id=invoice_id, entity_type="run", entity_id=run_id, actor=actor,
          status=res.status, exceptions=len(res.exceptions))
    conn.commit()
    return run_id


def get_result(conn, run_id) -> ReconResult:
    return ReconResult.model_validate_json(conn.execute("SELECT result_json FROM reconciliation_runs WHERE id=?", (run_id,)).fetchone()[0])


# ---------------- ingest uploaded / extracted documents ----------------
def ingest(conn, inv: Invoice, po: PO | None, grn: GRN | None, docs: dict, tol, actor="user") -> str:
    """docs: {'invoice': (name, bytes, method), 'po': ..., 'grn': ...} (any may be missing)."""
    ids = {k: save_document(conn, k, *v[:2], method=v[2]) for k, v in docs.items()}
    if po:
        insert_po(conn, po, ids.get("po"))
    if grn:
        insert_grn(conn, grn, ids.get("grn"))
    iid = insert_invoice(conn, inv, ids.get("invoice")) if inv else None   # PO/GRN-only uploads are allowed
    conn.commit()
    if iid:
        run_reconciliation(conn, iid, tol, actor)
    return iid


# ---------------- demo data ----------------
def load_scenario(conn, scn_id, tol) -> str:
    by_id = {s.id: s for s in samples.scenarios()}
    r = conn.execute("SELECT id FROM invoices WHERE demo_scenario=?", (scn_id,)).fetchone()
    if r:
        return r["id"]
    s = by_id[scn_id]
    for dep in s.requires:
        load_scenario(conn, dep, tol)
    insert_po(conn, s.po)
    if s.grn:
        insert_grn(conn, s.grn)
    iid = insert_invoice(conn, s.invoice, demo=s.id)
    audit(conn, "DEMO_LOADED", invoice_id=iid, scenario=s.id, title=s.title)
    conn.commit()
    run_reconciliation(conn, iid, tol)
    return iid


def load_all_demo(conn, tol):
    for s in samples.scenarios():
        load_scenario(conn, s.id, tol)


# ---------------- human review ----------------
def _flat(inv: Invoice) -> dict:
    d = {k: "" if v is None else str(v) for k, v in inv.model_dump(exclude={"lines"}).items()}
    for l in inv.lines:
        for k, v in l.model_dump().items():
            d[f"line[{l.line_no}].{k}"] = "" if v is None else str(v)
    return d


def apply_corrections(conn, invoice_id, new: Invoice, reason, reviewer, tol):
    if not reason.strip():
        raise ValueError("A reason is mandatory for field corrections.")
    a, b = _flat(load_invoice(conn, invoice_id)), _flat(new)
    changes = [(k, a.get(k, ""), b.get(k, "")) for k in sorted(set(a) | set(b)) if a.get(k, "") != b.get(k, "")]
    if not changes:
        raise ValueError("No changes to save.")
    for k, o, n in changes:  # originals stay in invoices.original_json; every change is logged here
        ins(conn, "field_corrections", id=uid(), invoice_id=invoice_id, field=k, previous_value=o, new_value=n,
            reason=reason, actor=reviewer, ts=now())
    upd(conn, "invoices", invoice_id, supplier_id=upsert_supplier(conn, new.supplier_name, new.supplier_gstin),
        norm_invoice_no=norm_inv_no(new.invoice_no), human_status=None, updated_at=now(), **{k: getattr(new, k) for k in HDR})
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (invoice_id,))
    _insert_lines(conn, invoice_id, new)
    audit(conn, "FIELD_CORRECTION", invoice_id=invoice_id, actor=reviewer, reason=reason, fields=[c[0] for c in changes])
    conn.commit()
    return run_reconciliation(conn, invoice_id, tol, reviewer)


def set_match_override(conn, invoice_id, line_no, po_line_ref, reason, reviewer, tol):
    if not reason.strip():
        raise ValueError("A reason is mandatory when resolving a match.")
    conn.execute("DELETE FROM line_match_overrides WHERE invoice_id=? AND line_no=?", (invoice_id, line_no))
    ins(conn, "line_match_overrides", id=uid(), invoice_id=invoice_id, line_no=line_no, po_line_ref=po_line_ref,
        reason=reason, actor=reviewer, ts=now())
    upd(conn, "invoices", invoice_id, human_status=None)
    audit(conn, "MATCH_OVERRIDE", invoice_id=invoice_id, actor=reviewer, line_no=line_no, po_line_ref=po_line_ref, reason=reason)
    conn.commit()
    return run_reconciliation(conn, invoice_id, tol, reviewer)


def decide(conn, invoice_id, decision, reason, reviewer):
    """Human decision only. The system never approves or pays on its own."""
    if decision not in ("HUMAN_APPROVED", "HUMAN_REJECTED"):
        raise ValueError("Unknown decision.")
    if not reason.strip():
        raise ValueError("A reason is mandatory.")
    row = conn.execute("SELECT status, latest_run_id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if decision == "HUMAN_APPROVED" and row["status"] in ("PENDING_DOCUMENTS", "REJECTED_BY_RULES"):
        raise ValueError(f"Cannot approve while system status is {row['status']} "
                         "(add the missing documents, or reject the invoice).")
    override = (decision == "HUMAN_APPROVED") != (row["status"] == "RECOMMENDED_FOR_APPROVAL")
    ins(conn, "review_decisions", id=uid(), invoice_id=invoice_id, run_id=row["latest_run_id"], decision=decision,
        system_status=row["status"], is_override=int(override), reason=reason, reviewer=reviewer, ts=now())
    upd(conn, "invoices", invoice_id, human_status=decision, updated_at=now())
    audit(conn, "REVIEW_DECISION", invoice_id=invoice_id, actor=reviewer, decision=decision, override=override, reason=reason)
    conn.commit()
