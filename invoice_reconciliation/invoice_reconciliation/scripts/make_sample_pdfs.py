"""Write synthetic sample PDFs (invoice/PO/GRN per scenario) to ./sample_documents for testing the upload flow.
Usage (from project root):  python -m scripts.make_sample_pdfs"""
from pathlib import Path

from core import samples

out = Path("sample_documents")
for s in samples.scenarios():
    folder = out / f"{s.id}_{s.title.lower().replace(' ', '_').replace('(', '').replace(')', '')[:40]}"
    folder.mkdir(parents=True, exist_ok=True)
    for kind, model in (("invoice", s.invoice), ("po", s.po), ("grn", s.grn)):
        if model is not None:
            (folder / f"{kind}.pdf").write_bytes(samples.render_pdf(samples.render_text(kind, model)))
print(f"Sample PDFs written to {out.resolve()}")
