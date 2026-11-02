"""Intelligent Invoice Reconciliation & Exception Detection - Streamlit prototype (SYNTHETIC DEMO DATA ONLY).
UI only: all decisions come from core.reconcile (deterministic); humans make the final call."""
import json
from collections import Counter, defaultdict
from decimal import Decimal

import pandas as pd
import streamlit as st

import config
from config import Tolerances
from core import db, extraction, samples, service
from core.models import GRN, Invoice, PO

st.set_page_config(page_title="Invoice Reconciliation Prototype", page_icon="🧾", layout="wide")


@st.cache_resource
def _init() -> bool:
    db.init_db()
    return True


_init()
conn = db.connect()

STATUS = {"RECOMMENDED_FOR_APPROVAL": "✅ RECOMMENDED_FOR_APPROVAL", "REQUIRES_REVIEW": "⚠️ REQUIRES_REVIEW",
          "PENDING_DOCUMENTS": "⏳ PENDING_DOCUMENTS", "REJECTED_BY_RULES": "⛔ REJECTED_BY_RULES", None: "—"}
HUMAN = {"HUMAN_APPROVED": "👍 HUMAN_APPROVED", "HUMAN_REJECTED": "👎 HUMAN_REJECTED", None: "none yet"}
SEV = {"CRITICAL": "🔴 CRITICAL", "HIGH": "🟠 HIGH", "MEDIUM": "🟡 MEDIUM", "LOW": "🔵 LOW", "INFO": "⚪ INFO"}
SEV_RANK = {k: i for i, k in enumerate(SEV)}
PAGES = ["Dashboard", "Upload Documents", "Invoice Reconciliation", "Exceptions", "Human Review", "Audit History", "Reports"]
HDR_FIELDS = ["invoice_no", "invoice_date", "supplier_name", "supplier_gstin", "buyer_gstin", "po_number", "currency",
              "subtotal", "cgst_total", "sgst_total", "igst_total", "freight", "other_charges", "round_off", "grand_total"]


def inr(x) -> str:
    x = Decimal(x).quantize(Decimal("0.01"))
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + parts + [tail])
    return f"{'-' if x < 0 else ''}₹{whole}.{frac}"


def s(x) -> str:
    return "—" if x is None else str(x)


def ok(b: bool) -> str:
    return "✅" if b else "❌"


def show(df: pd.DataFrame, flag_col: str = "OK") -> None:
    if flag_col in df.columns and not df.empty:
        df = df.style.apply(lambda r: ["background-color: #ffe0e0" if r[flag_col] == "❌" else ""] * len(r), axis=1)
    st.dataframe(df, use_container_width=True, hide_index=True)


def csv_button(label: str, df: pd.DataFrame, name: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8"), file_name=name, mime="text/csv")


def flash(msg: str) -> None:
    st.session_state["_flash"] = msg


def pick_invoice():
    rows = conn.execute("SELECT i.id, i.invoice_no, i.status, i.demo_scenario, s.name FROM invoices i "
                        "JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.seq DESC").fetchall()
    if not rows:
        st.info("No invoices yet - go to **Upload Documents** and load the demo scenarios.")
        return None
    ids = [r["id"] for r in rows]
    cur = st.session_state.get("_inv")
    labels = {r["id"]: f"{r['demo_scenario'] + ' · ' if r['demo_scenario'] else ''}{r['invoice_no']} · {r['name']} · {STATUS[r['status']]}"
              for r in rows}
    sel = st.selectbox("Invoice", ids, index=ids.index(cur) if cur in ids else 0, format_func=labels.get)
    st.session_state["_inv"] = sel
    return sel


def sidebar():
    st.sidebar.title("🧾 Invoice Reconciliation")
    st.sidebar.caption("Prototype · SYNTHETIC DEMO DATA ONLY · Not an ERP, payment, fraud-detection or GST-compliance tool.")
    if config.llm_enabled():
        st.sidebar.warning(f"External AI configured: OpenRouter ({config.OPENROUTER_MODEL}). Document text is sent there "
                           "only when you tick the consent box on the Upload page.")
    else:
        st.sidebar.info("No AI provider configured: documents are processed locally (PDF text + demo fallback parser).")
    page = st.sidebar.radio("Go to", PAGES)
    reviewer = st.sidebar.text_input("Reviewer name", value="reviewer")
    t = Tolerances()
    with st.sidebar.expander("Tolerances"):
        pp = st.number_input("Price tolerance (%)", 0.0, 100.0, float(t.price_pct), 0.1)
        ta = st.number_input("Invoice total tolerance (₹)", 0.0, 1000.0, float(t.total_abs), 0.5)
        tx = st.number_input("Tax rounding tolerance (₹ per line)", 0.0, 100.0, float(t.tax_abs), 0.01)
        st.caption("Applied the next time a reconciliation runs.")
    return page, reviewer, Tolerances(price_pct=Decimal(str(pp)), total_abs=Decimal(str(ta)), tax_abs=Decimal(str(tx)),
                                      line_abs=t.line_abs)


# ------------------------------------------------------------------ Dashboard
def page_dashboard():
    st.header("Dashboard")
    rows = [dict(r) for r in conn.execute("SELECT i.*, s.name supplier FROM invoices i JOIN suppliers s ON s.id=i.supplier_id")]
    if not rows:
        st.info("No invoices yet. Open **Upload Documents** and click *Load all demo scenarios*.")
        return
    cnt = Counter(r["status"] for r in rows)
    total = sum((Decimal(r["grand_total"]) for r in rows), Decimal(0))
    metrics = [("Invoices processed", len(rows)), ("Total invoice value", inr(total)),
               ("Pending documents", cnt["PENDING_DOCUMENTS"]), ("Requires review", cnt["REQUIRES_REVIEW"]),
               ("Recommended for approval", cnt["RECOMMENDED_FOR_APPROVAL"]), ("Rejected by rules", cnt["REJECTED_BY_RULES"])]
    for col, (label, val) in zip(st.columns(6), metrics):
        col.metric(label, val)
    hc = Counter(r["human_status"] for r in rows)
    st.caption(f"Human decisions so far: {hc['HUMAN_APPROVED']} approved · {hc['HUMAN_REJECTED']} rejected · "
               f"{len(rows) - hc['HUMAN_APPROVED'] - hc['HUMAN_REJECTED']} awaiting a decision. "
               "System statuses are recommendations only; nothing is paid automatically.")
    left, right = st.columns(2)
    with left:
        st.subheader("Exception categories")
        ex = pd.DataFrame([dict(r) for r in conn.execute(
            "SELECT e.category, COUNT(*) AS count FROM exceptions e JOIN invoices i ON i.latest_run_id=e.run_id "
            "WHERE e.severity != 'INFO' GROUP BY e.category ORDER BY count DESC")])
        st.write("No exceptions.") if ex.empty else st.bar_chart(ex.set_index("category"))
    with right:
        st.subheader("Supplier summary")
        agg = defaultdict(lambda: {"Invoices": 0, "Value": Decimal(0), "Needs attention": 0})
        for r in rows:
            a = agg[r["supplier"]]
            a["Invoices"] += 1
            a["Value"] += Decimal(r["grand_total"])
            a["Needs attention"] += r["status"] != "RECOMMENDED_FOR_APPROVAL"
        show(pd.DataFrame([{"Supplier": k, "Invoices": v["Invoices"], "Total value": inr(v["Value"]),
                            "Needs attention": v["Needs attention"]} for k, v in agg.items()]))


# ------------------------------------------------------------------ Upload
def page_upload(tol, reviewer):
    st.header("Upload Documents")
    st.subheader("1 · Demo scenarios (synthetic, fictional data)")
    scs = samples.scenarios()
    by_id = {x.id: x for x in scs}
    show(pd.DataFrame([{"ID": x.id, "Scenario": x.title, "What it shows": x.description, "Expected outcome": x.expected}
                       for x in scs]))
    c1, c2, c3 = st.columns([1, 2, 2])
    if c1.button("Load all demo scenarios", type="primary"):
        service.load_all_demo(conn, tol)
        st.success("Demo scenarios loaded and reconciled. See Dashboard.")
    pick = c2.selectbox("Single scenario", list(by_id), format_func=lambda i: f"{i} · {by_id[i].title}")
    if c2.button("Load selected scenario"):
        service.load_scenario(conn, pick, tol)
        st.success(f"{pick} loaded (with any scenarios it depends on).")
    with c3:
        st.caption("Sample PDFs for this scenario (to test the upload flow below):")
        try:
            sc = by_id[pick]
            for kind, model in (("invoice", sc.invoice), ("po", sc.po), ("grn", sc.grn)):
                if model is not None:
                    st.download_button(f"⬇ {kind}.pdf", samples.render_pdf(samples.render_text(kind, model)),
                                       file_name=f"{pick}_{kind}.pdf", mime="application/pdf", key=f"dl_{pick}_{kind}")
        except ImportError:
            st.caption("Install PyMuPDF to generate sample PDFs.")

    st.divider()
    st.subheader("2 · Upload your own PDFs")
    if config.llm_enabled():
        use_llm = st.checkbox(f"Send extracted document text to OpenRouter ({config.OPENROUTER_MODEL}) for AI extraction",
                              value=False)
        if use_llm:
            st.warning("External LLM processing is ON: document text leaves this machine. Use synthetic/non-sensitive documents only.")
    else:
        use_llm = False
        st.info("No AI provider configured. The offline fallback parser only understands the synthetic demo PDF layout; "
                "other PDFs need AI extraction (set OPENROUTER_API_KEY in .env) or manual entry. Scanned PDFs need OCR, which is not available.")
    files = {k: st.file_uploader(lbl, type=["pdf"], key=f"up_{k}")
             for k, lbl in (("invoice", "Invoice PDF"), ("po", "Purchase Order PDF"), ("grn", "Goods Receipt Note PDF"))}
    if st.button("Extract fields"):
        ex = {}
        for k, f in files.items():
            if f is None:
                continue
            data = f.getvalue()
            o = extraction.extract(k, data, use_llm)
            db.audit(conn, "EXTRACTION_ATTEMPT", actor=reviewer, kind=k, filename=f.name, method=o.method,
                     ok=o.error is None, error=o.error)
            conn.commit()
            ex[k] = {"name": f.name, "bytes": data, "method": o.method, "error": o.error}
            st.session_state[f"json_{k}"] = (json.dumps(o.model.model_dump(mode="json"), indent=2) if o.model
                                             else extraction.blank_template(k))
        st.session_state["extracted"] = ex
    ex = st.session_state.get("extracted") or {}
    for k, v in ex.items():
        label = "🤖 AI extraction (OpenRouter)" if v["method"] == extraction.METHOD_LLM else "🧪 Demo fallback parser (not AI)"
        st.markdown(f"**{k.upper()}** · `{v['name']}` · {label}")
        if v["error"]:
            st.error(v["error"] + " A blank template is shown below - fill it in manually.")
        st.text_area("Extracted / editable JSON (review and correct before saving)", key=f"json_{k}", height=260)
    if ex and st.button("Save & reconcile", type="primary"):
        try:
            parsed = {k: json.loads(st.session_state[f"json_{k}"], parse_float=Decimal) for k in ex}
            inv = Invoice.model_validate(parsed["invoice"]) if "invoice" in parsed else None
            po = PO.model_validate(parsed["po"]) if "po" in parsed else None
            grn = GRN.model_validate(parsed["grn"]) if "grn" in parsed else None
        except ValueError as e:  # includes JSON errors and pydantic validation errors
            st.error(f"Cannot save - please fix the JSON: {e}")
        else:
            docs = {k: (v["name"], v["bytes"], v["method"]) for k, v in ex.items()}
            iid = service.ingest(conn, inv, po, grn, docs, tol, reviewer)
            if iid:
                st.session_state["_inv"] = iid
            st.session_state["extracted"] = {}
            flash("Saved. " + ("Invoice reconciled - open Invoice Reconciliation." if iid else
                               "PO/GRN stored - re-run any pending invoice from Invoice Reconciliation."))
            st.rerun()


# ------------------------------------------------------------------ Reconciliation
def page_recon(tol, reviewer):
    st.header("Invoice Reconciliation")
    iid = pick_invoice()
    if not iid:
        return
    row = conn.execute("SELECT i.*, s.name supplier, s.gstin gstin FROM invoices i JOIN suppliers s ON s.id=i.supplier_id "
                       "WHERE i.id=?", (iid,)).fetchone()
    runs = conn.execute("SELECT id, run_at, status FROM reconciliation_runs WHERE invoice_id=? ORDER BY run_at DESC, rowid DESC",
                        (iid,)).fetchall()
    a, b = st.columns([4, 1])
    run_labels = {r["id"]: f"{r['run_at']} · {r['status']}" + (" (latest)" if i == 0 else "") for i, r in enumerate(runs)}
    run_id = a.selectbox("Reconciliation run (previous runs are preserved)", list(run_labels), format_func=run_labels.get)
    if b.button("Re-run now"):
        service.run_reconciliation(conn, iid, tol, reviewer)
        st.rerun()
    res = service.get_result(conn, run_id)

    st.markdown(f"### {STATUS[res.status]}")
    st.caption(res.explanation)
    st.write(f"Human decision: **{HUMAN[row['human_status']]}**" + ("  ·  🏷 synthetic demo data" if row["demo_scenario"] else ""))
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Supplier**  \n{row['supplier']}  \nGSTIN: `{row['gstin']}`")
    c2.markdown(f"**Invoice**  \n{row['invoice_no']}  \nDate: {row['invoice_date']}  \nPO ref: {s(row['po_number'])} · {row['currency']}")
    c3.markdown(f"**Declared grand total**  \n{inr(row['grand_total'])}  \nExceptions: {len(res.exceptions)}")

    t1, t2, t3, t4 = st.tabs(["PO & GRN comparison", "Line items", "Tax calculation", "Invoice totals"])
    with t1:
        show(pd.DataFrame([{"Check": c.check, "Expected": c.expected, "Invoice": c.actual, "OK": ok(c.ok)} for c in res.doc_checks]))
        po = service.load_po(conn, row["po_number"]) if row["po_number"] else None
        if po is None:
            st.warning("No purchase order on file for this invoice.")
        else:
            grn = service.load_grn_lines(conn, po.po_number) or []
            rec, acc, inv_q = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
            for g in grn:
                rec[g.po_line_ref] += g.received_qty
                acc[g.po_line_ref] += g.accepted_qty
            for l in res.lines:
                if l.po_line_ref:
                    inv_q[l.po_line_ref] += l.inv_qty
            st.caption(f"Quantities by PO line, using the PO/GRNs currently on file ({po.po_number}):")
            show(pd.DataFrame([{"PO line": p.line_ref, "Description": p.description, "Ordered": s(p.qty), "Received": s(rec[p.line_ref]),
                                "Accepted (GRN)": s(acc[p.line_ref]), "Invoiced": s(inv_q[p.line_ref]), "Unit": p.unit,
                                "OK": ok(inv_q[p.line_ref] <= min(p.qty, acc[p.line_ref]) or inv_q[p.line_ref] == 0)}
                               for p in po.lines]))
    with t2:
        show(pd.DataFrame([{"Line": l.line_no, "Invoice description": l.description, "Match": l.match_status,
                            "PO line": s(l.po_line_ref), "Inv qty": s(l.inv_qty), "PO qty": s(l.po_qty),
                            "GRN accepted": s(l.grn_accepted), "Not received": s(l.not_received), "Inv price": s(l.inv_price),
                            "PO price": s(l.po_price), "Price var %": s(l.price_var_pct), "Issues": ", ".join(l.flags) or "—",
                            "OK": ok(not l.flags)} for l in res.lines]))
        for l in res.lines:
            if l.candidates:
                st.info(f"Line {l.line_no} candidates: " + " | ".join(f"[{c.line_ref}] {c.description} ({c.score})" for c in l.candidates))
    with t3:
        st.caption(f"Supply treated as **{'inter-state (IGST)' if res.interstate else 'intra-state (CGST + SGST)'}** from supplier/buyer GSTIN state codes. "
                   "Arithmetic recalculation only - not a GST compliance certification.")
        show(pd.DataFrame([{"Line": t.line_no, "Tax": t.component, "Taxable value": s(t.taxable), "Rate %": s(t.rate),
                            "Declared": s(t.declared), "Recalculated": s(t.calculated), "Difference": s(t.difference),
                            "OK": ok(t.ok)} for t in res.tax_rows]))
    with t4:
        show(pd.DataFrame([{"Item": t.item, "Declared": s(t.declared), "Recomputed from lines": s(t.calculated),
                            "Difference": s(t.difference), "Tolerance": s(t.tolerance), "OK": ok(t.ok)} for t in res.total_rows]))


# ------------------------------------------------------------------ Exceptions
def page_exceptions():
    st.header("Exceptions")
    scope = st.radio("Scope", ["Selected invoice", "All invoices"], horizontal=True)
    sql = ("SELECT e.*, i.invoice_no FROM exceptions e JOIN invoices i ON i.latest_run_id=e.run_id")
    if scope == "Selected invoice":
        iid = pick_invoice()
        if not iid:
            return
        rows = conn.execute(sql + " WHERE i.id=?", (iid,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    sev = st.multiselect("Severity", list(SEV), default=list(SEV))
    rows = sorted([r for r in rows if r["severity"] in sev], key=lambda r: (SEV_RANK[r["severity"]], r["invoice_no"], r["line_no"] or 0))
    if not rows:
        st.success("No exceptions for this selection.")
        return
    show(pd.DataFrame([{"Invoice": r["invoice_no"], "Code": r["code"], "Severity": SEV[r["severity"]], "Line": s(r["line_no"]),
                        "Description": r["description"], "Expected": s(r["expected"]), "Actual": s(r["actual"]),
                        "Difference": s(r["difference"])} for r in rows]), flag_col="_")


# ------------------------------------------------------------------ Human review
def page_review(tol, reviewer):
    st.header("Human Review")
    iid = pick_invoice()
    if not iid:
        return
    n = st.session_state.get("_n", 0)   # bumps after each save so widgets reset
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
    inv = service.load_invoice(conn, iid)
    res = service.get_result(conn, row["latest_run_id"])
    st.markdown(f"**System recommendation:** {STATUS[row['status']]}  ·  **Human decision:** {HUMAN[row['human_status']]}")
    st.caption(res.explanation)
    if res.exceptions:
        show(pd.DataFrame([{"Severity": SEV[e.severity], "Code": e.code, "Line": s(e.line_no), "Description": e.description}
                           for e in res.exceptions]), flag_col="_")
    t1, t2, t3 = st.tabs(["Decision", "Resolve PO line matches", "Correct extracted fields"])

    with t1:
        reason = st.text_area("Reason (mandatory for every decision; explains any override of the system recommendation)",
                              key=f"reason_{iid}_{n}")
        blocked = row["status"] in ("PENDING_DOCUMENTS", "REJECTED_BY_RULES")
        if blocked:
            st.warning("Approval is blocked while documents are missing or a hard-stop rule has fired. You can still reject.")
        ca, cb = st.columns(2)
        for col, label, decision, dis in ((ca, "✅ Approve invoice", "HUMAN_APPROVED", blocked), (cb, "❌ Reject invoice", "HUMAN_REJECTED", False)):
            if col.button(label, disabled=dis, key=f"{decision}_{iid}_{n}"):
                try:
                    service.decide(conn, iid, decision, reason, reviewer)
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.session_state["_n"] = n + 1
                    flash(f"Decision recorded: {decision}. (No payment is initiated by this system.)")
                    st.rerun()
        d = pd.DataFrame([dict(r) for r in conn.execute(
            "SELECT ts AS Time, decision AS Decision, system_status AS 'System status', is_override AS Override, reason AS Reason, "
            "reviewer AS Reviewer FROM review_decisions WHERE invoice_id=? ORDER BY ts DESC, rowid DESC", (iid,))])
        if not d.empty:
            d["Override"] = d["Override"].map(lambda v: "yes" if v else "no")
            show(d)

    with t2:
        po = service.load_po(conn, inv.po_number) if inv.po_number else None
        todo = [l for l in res.lines if l.match_status in ("AMBIGUOUS", "NO_MATCH", "OVERRIDDEN")]
        if po is None:
            st.info("No PO on file, so there is nothing to match against.")
        elif not todo:
            st.success("All lines matched automatically.")
        for l in todo:
            with st.expander(f"Line {l.line_no} · {l.description} · {l.match_status}", expanded=l.match_status != "OVERRIDDEN"):
                if l.candidates:
                    st.write("Candidates: " + " | ".join(f"[{c.line_ref}] {c.description} (score {c.score})" for c in l.candidates))
                refs = [p.line_ref for p in po.lines]
                lab = {p.line_ref: f"[{p.line_ref}] {p.description} — {p.qty} {p.unit} @ {p.unit_price}" for p in po.lines}
                default = refs.index(l.po_line_ref) if l.po_line_ref in refs else (refs.index(l.candidates[0].line_ref) if l.candidates else 0)
                choice = st.selectbox("Matches PO line", refs, index=default, format_func=lab.get, key=f"m_{iid}_{l.line_no}_{n}")
                why = st.text_input("Reason (mandatory)", key=f"mr_{iid}_{l.line_no}_{n}")
                if st.button("Apply match & re-run", key=f"mb_{iid}_{l.line_no}_{n}"):
                    try:
                        service.set_match_override(conn, iid, l.line_no, choice, why, reviewer, tol)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        st.session_state["_n"] = n + 1
                        flash(f"Line {l.line_no} matched to PO line {choice}; reconciliation re-run.")
                        st.rerun()

    with t3:
        st.caption("Corrections are saved as new current values; the originally extracted data and every change stay on record.")
        vals, cols = {}, st.columns(3)
        for i, k in enumerate(HDR_FIELDS):
            v = getattr(inv, k)
            vals[k] = cols[i % 3].text_input(k, value="" if v is None else str(v), key=f"h_{iid}_{k}_{n}")
        df = pd.DataFrame([{k: "" if v is None else str(v) for k, v in l.model_dump().items()} for l in inv.lines])
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"le_{iid}_{n}")
        why = st.text_area("Reason for correction (mandatory)", key=f"cr_{iid}_{n}")
        if st.button("Save corrections & re-run", key=f"cs_{iid}_{n}"):
            try:
                lines = [{k: (None if (v is None or pd.isna(v) or str(v).strip() == "") else str(v).strip()) for k, v in r.items()}
                         for r in edited.to_dict("records")]
                new = Invoice.model_validate({**{k: (v.strip() or None) for k, v in vals.items()}, "lines": lines})
                service.apply_corrections(conn, iid, new, why, reviewer, tol)
            except ValueError as e:
                st.error(f"Not saved: {e}")
            else:
                st.session_state["_n"] = n + 1
                flash("Corrections saved and reconciliation re-run.")
                st.rerun()
        with st.expander("Original extracted values (never overwritten)"):
            st.json(json.loads(row["original_json"]))
        fc = pd.DataFrame([dict(r) for r in conn.execute(
            "SELECT ts AS Time, field AS Field, previous_value AS Before, new_value AS After, reason AS Reason, actor AS By "
            "FROM field_corrections WHERE invoice_id=? ORDER BY ts DESC, rowid DESC", (iid,))])
        if not fc.empty:
            st.write("Correction history")
            show(fc)


# ------------------------------------------------------------------ Audit
def audit_df(invoice_id=None, types=None) -> pd.DataFrame:
    q = ("SELECT a.ts AS Time, a.event_type AS Event, i.invoice_no AS Invoice, a.actor AS Actor, a.details_json AS Details "
         "FROM audit_events a LEFT JOIN invoices i ON i.id=a.invoice_id")
    where, args = [], []
    if invoice_id:
        where.append("a.invoice_id=?")
        args.append(invoice_id)
    if types:
        where.append(f"a.event_type IN ({','.join('?' * len(types))})")
        args += list(types)
    q += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.ts DESC, a.rowid DESC"
    return pd.DataFrame([dict(r) for r in conn.execute(q, args)])


def page_audit():
    st.header("Audit History")
    inv_rows = conn.execute("SELECT id, invoice_no FROM invoices ORDER BY seq DESC").fetchall()
    names = {None: "All events", **{r["id"]: r["invoice_no"] for r in inv_rows}}
    sel = st.selectbox("Invoice", list(names), format_func=names.get)
    all_types = [r[0] for r in conn.execute("SELECT DISTINCT event_type FROM audit_events ORDER BY 1")]
    types = st.multiselect("Event types", all_types, default=all_types)
    df = audit_df(sel, types)
    st.write("No events yet.") if df.empty else show(df)


# ------------------------------------------------------------------ Reports
def page_reports():
    st.header("Reports")
    inv = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT i.demo_scenario AS demo_scenario, i.invoice_no, s.name AS supplier, s.gstin AS supplier_gstin, i.invoice_date, i.po_number, "
        "i.grand_total, i.status AS system_status, i.human_status, "
        "(SELECT COUNT(*) FROM exceptions e WHERE e.run_id=i.latest_run_id AND e.severity!='INFO') AS exceptions "
        "FROM invoices i JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.seq")])
    if inv.empty:
        st.info("Nothing to report yet.")
        return
    st.subheader("Invoice register")
    show(inv)
    c1, c2, c3 = st.columns(3)
    with c1:
        csv_button("⬇ Invoice register (CSV)", inv, "invoice_register.csv")
    exc = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT i.invoice_no, e.code, e.severity, e.category, e.line_no, e.description, e.expected, e.actual, e.difference "
        "FROM exceptions e JOIN invoices i ON i.latest_run_id=e.run_id ORDER BY i.seq, e.severity")])
    with c2:
        csv_button("⬇ Exceptions (CSV)", exc, "exceptions.csv")
    with c3:
        csv_button("⬇ Audit trail (CSV)", audit_df(), "audit_trail.csv")
    g1, g2 = st.columns(2)
    with g1:
        st.subheader("Invoices by system status")
        st.bar_chart(inv["system_status"].value_counts())
    with g2:
        st.subheader("Exceptions by severity")
        st.write("No exceptions.") if exc.empty else st.bar_chart(exc["severity"].value_counts())
    if not exc.empty:
        st.subheader("Exceptions by code")
        st.bar_chart(exc["code"].value_counts())


# ------------------------------------------------------------------ router
page, reviewer, tol = sidebar()
if "_flash" in st.session_state:
    st.success(st.session_state.pop("_flash"))
{"Dashboard": page_dashboard, "Upload Documents": lambda: page_upload(tol, reviewer),
 "Invoice Reconciliation": lambda: page_recon(tol, reviewer), "Exceptions": page_exceptions,
 "Human Review": lambda: page_review(tol, reviewer), "Audit History": page_audit, "Reports": page_reports}[page]()
