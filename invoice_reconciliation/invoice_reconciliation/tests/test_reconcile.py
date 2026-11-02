from decimal import Decimal as D

import pytest

from config import Tolerances
from core import db, samples, service
from core.models import ReconResult

TOL = Tolerances()


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path / "t.db")
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def _inv(conn, sid):
    return conn.execute("SELECT * FROM invoices WHERE demo_scenario=?", (sid,)).fetchone()


def _codes(conn, sid):
    r = _inv(conn, sid)
    return {x["code"] for x in conn.execute("SELECT code FROM exceptions WHERE run_id=?", (r["latest_run_id"],))}


def test_all_scenarios_hit_expected_status(conn):
    service.load_all_demo(conn, TOL)
    for s in samples.scenarios():
        assert _inv(conn, s.id)["status"] == s.expected, s.id


def test_expected_exception_codes(conn):
    service.load_all_demo(conn, TOL)
    assert _codes(conn, "S01") == set()
    assert "QTY_EXCEEDS_PO" in _codes(conn, "S02")
    assert _codes(conn, "S03") == {"QTY_NOT_RECEIVED"}
    assert _codes(conn, "S04") == {"PRICE_MISMATCH"}
    assert _codes(conn, "S05") == {"TAX_MISMATCH"}          # tax error only; line arithmetic and totals stay clean
    assert _codes(conn, "S06") == {"TOTAL_MISMATCH"}
    assert "DUPLICATE_EXACT" in _codes(conn, "S07")
    assert _codes(conn, "S08") == {"MISSING_GRN"}
    assert _codes(conn, "S09") == {"AMBIGUOUS_MATCH"}
    assert _codes(conn, "S10") == {"UNAUTHORIZED_CHARGES"}
    assert _codes(conn, "S11") == {"DUPLICATE_POTENTIAL"}
    assert _codes(conn, "S12") == set()                     # partial delivery billed for received qty is fine


def test_partial_delivery_flags_30_units(conn):
    service.load_all_demo(conn, TOL)
    res = service.get_result(conn, _inv(conn, "S03")["latest_run_id"])
    assert res.lines[0].not_received == D("30")
    assert res.status == "REQUIRES_REVIEW"


def test_resolve_ambiguous_match_then_clean(conn):
    service.load_all_demo(conn, TOL)
    iid = _inv(conn, "S09")["id"]
    service.set_match_override(conn, iid, 1, "2", "Matches 80mm bolt per supplier email", "tester", TOL)
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (iid,)).fetchone()[0] == "RECOMMENDED_FOR_APPROVAL"
    assert conn.execute("SELECT COUNT(*) FROM reconciliation_runs WHERE invoice_id=?", (iid,)).fetchone()[0] == 2  # history kept


def test_decisions_need_reason_and_respect_hard_stops(conn):
    service.load_all_demo(conn, TOL)
    with pytest.raises(ValueError):
        service.decide(conn, _inv(conn, "S02")["id"], "HUMAN_APPROVED", "  ", "t")
    with pytest.raises(ValueError):
        service.decide(conn, _inv(conn, "S07")["id"], "HUMAN_APPROVED", "ok", "t")
    with pytest.raises(ValueError):
        service.decide(conn, _inv(conn, "S08")["id"], "HUMAN_APPROVED", "ok", "t")
    service.decide(conn, _inv(conn, "S02")["id"], "HUMAN_APPROVED", "Extra reams accepted by manager", "t")
    d = conn.execute("SELECT is_override FROM review_decisions").fetchone()
    assert d["is_override"] == 1
    assert _inv(conn, "S02")["status"] == "REQUIRES_REVIEW"   # system status is never rewritten by a human


def test_corrections_preserve_original(conn):
    service.load_all_demo(conn, TOL)
    iid = _inv(conn, "S06")["id"]
    inv = service.load_invoice(conn, iid)
    original = _inv(conn, "S06")["original_json"]
    inv.grand_total -= D("250.00")
    service.apply_corrections(conn, iid, inv, "Typo in grand total", "tester", TOL)
    assert _inv(conn, "S06")["original_json"] == original
    assert _inv(conn, "S06")["status"] == "RECOMMENDED_FOR_APPROVAL"
    assert conn.execute("SELECT COUNT(*) FROM field_corrections WHERE invoice_id=?", (iid,)).fetchone()[0] == 1


def test_fallback_parser_roundtrip():
    from core import extraction
    for s in samples.scenarios()[:3]:
        for kind, m in (("invoice", s.invoice), ("po", s.po), ("grn", s.grn)):
            raw = extraction.parse_demo_text(kind, samples.render_text(kind, m))
            assert type(m).model_validate(raw).model_dump() == m.model_dump(), (s.id, kind)


def test_unparseable_text_gives_clear_error():
    from core import extraction
    with pytest.raises(extraction.ExtractionError):
        extraction.parse_demo_text("invoice", "random text from some scanned vendor")
