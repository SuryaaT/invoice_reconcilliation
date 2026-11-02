# Intelligent Invoice Reconciliation & Exception Detection (prototype)

AI-assisted **three-way match** (Purchase Order ↔ Goods Receipt Note ↔ Supplier Invoice) for Indian SMEs, with a human review workflow.
**Synthetic demo data only.** This is a prototype - not an ERP/accounting system, it never approves or pays anything, and it makes no
fraud-detection or GST-compliance claims (tax checks are arithmetic recalculation only; GSTIN is a format check, not portal verification).

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # optional: add OPENROUTER_API_KEY
streamlit run app.py
```
Then open **Upload Documents → Load all demo scenarios**. Run the tests with `pytest`.
Optional: `python -m scripts.make_sample_pdfs` writes sample PDFs (also downloadable per scenario in the app) to try the upload flow.

## How it works
| Layer | File | Role |
|---|---|---|
| UI | `app.py` | Streamlit pages: Dashboard, Upload, Reconciliation, Exceptions, Human Review, Audit, Reports |
| Rules | `core/reconcile.py`, `core/matching.py` | **All** financial logic. Pure functions, `Decimal` only, collects *every* exception |
| AI | `core/extraction.py` → `ai_extract()` | The only LLM call; extraction only. Output validated by Pydantic; failures → error + manual correction |
| Storage | `core/db.py`, `core/service.py` | SQLite (non-destructive init, foreign keys on), documents saved under `data/documents/` |
| Data | `core/samples.py` | 12 fictional scenarios + renderer for sample PDFs |

**Outcomes** (system): `REJECTED_BY_RULES` (exact duplicate) · `PENDING_DOCUMENTS` (PO/GRN missing) · `REQUIRES_REVIEW` · `RECOMMENDED_FOR_APPROVAL`.
**Human decisions** (separate): `HUMAN_APPROVED` / `HUMAN_REJECTED`, always with a reason. Approval is blocked for pending/hard-stopped invoices.

**Checks:** quantity vs PO and vs accepted GRN (partial deliveries flagged, not auto-rejected) · price vs PO (default 0.5%) · CGST/SGST/IGST recalculation ·
line arithmetic (kept separate from tax) · invoice totals (default ±₹1) · exact/potential duplicates · missing documents · product/ambiguous match ·
unit of measure · unauthorized freight/charges · currency · supplier mismatch.
Tolerances: `.env` or the app sidebar.

## Demo scenarios
S01 perfect match · S02 qty > PO · S03 qty > received (30 units flagged) · S04 price too high · S05 wrong GST · S06 total arithmetic error ·
S07 exact duplicate · S08 missing GRN · S09 ambiguous product (resolve in Human Review) · S10 unauthorized freight ·
S11 potential duplicate · S12 partial delivery billed correctly.

## AI & privacy
- No API key → fully offline: PDF text via PyMuPDF + a **clearly labelled demo fallback parser** that only reads the synthetic PDF layout.
- With `OPENROUTER_API_KEY`, document text is sent to OpenRouter **only if you tick the consent box** on the Upload page; the UI says so.
- Document text is treated as untrusted data (prompted as such; the LLM output is never executed and never decides anything). Missing values stay `null` → validation error → manual correction.
- Scanned PDFs: OCR is not configured; the app says so instead of pretending.

## Known limitations (deliberate, to keep the prototype small)
- Invoiced quantity is compared per invoice; it is **not** accumulated across several invoices against one PO line (GRN quantities *are* summed across GRNs). Potential-duplicate detection partly mitigates double billing.
- Freight/other charges are treated as untaxed amounts; no TDS/TCS/cess/reverse charge/e-invoice (IRN) logic.
- Place of supply is inferred from the first two GSTIN digits.
- No authentication/roles; the reviewer name is free text. Single-user SQLite.
