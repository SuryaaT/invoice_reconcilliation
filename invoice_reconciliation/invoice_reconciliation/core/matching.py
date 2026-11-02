"""Simple, explainable invoice-line -> PO-line matching (no embeddings)."""
import re
from difflib import SequenceMatcher

from .models import InvoiceLine, POLine

_STOP = {"the", "of", "and", "for", "with", "x", "a", "an"}


def tokens(text: str) -> list[str]:
    out = []
    for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split():
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural stripping: bolts -> bolt
        out.append(t)
    return out


def norm_desc(text: str) -> str:
    return " ".join(tokens(text))


def score(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return 0.5 * (jac + ratio)


def match_line(line: InvoiceLine, po_lines: list[POLine], override_ref: str | None = None):
    """Returns (status, po_line | None, candidates[(score, POLine)], best_score).
    Priority: human override > PO line reference > description (+HSN bonus)."""
    by_ref = {p.line_ref: p for p in po_lines}
    if override_ref and override_ref in by_ref:
        p = by_ref[override_ref]
        return "OVERRIDDEN", p, [], score(line.description, p.description)
    if line.po_line_ref and line.po_line_ref in by_ref:
        p = by_ref[line.po_line_ref]
        return "MATCHED", p, [], score(line.description, p.description)

    scored = []
    for p in po_lines:
        s = score(line.description, p.description)
        if line.hsn and p.hsn and line.hsn == p.hsn:
            s += 0.15
        scored.append((min(s, 1.0), p))
    scored.sort(key=lambda x: -x[0])
    cands = [c for c in scored if c[0] >= 0.45]
    if not cands:
        return "NO_MATCH", None, [], 0.0
    top = cands[0][0]
    second = cands[1][0] if len(cands) > 1 else 0.0
    if top >= 0.6 and top - second >= 0.15:
        return "MATCHED", cands[0][1], [], top
    return "AMBIGUOUS", None, cands[:5], top
