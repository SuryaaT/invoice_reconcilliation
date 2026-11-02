"""Pydantic models: extracted documents + reconciliation results. Money/quantities are Decimal only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _B(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


# ---------- documents (what AI / parser / human supplies) ----------
class InvoiceLine(_B):
    line_no: int
    po_line_ref: Optional[str] = None
    description: str
    hsn: Optional[str] = None
    qty: Decimal = Field(ge=0)
    unit: str
    unit_price: Decimal = Field(ge=0)
    taxable_value: Optional[Decimal] = None
    gst_rate: Optional[Decimal] = None
    cgst: Optional[Decimal] = None
    sgst: Optional[Decimal] = None
    igst: Optional[Decimal] = None
    line_total: Optional[Decimal] = None


class Invoice(_B):
    invoice_no: str
    invoice_date: date
    supplier_name: str
    supplier_gstin: str
    buyer_gstin: Optional[str] = None
    po_number: Optional[str] = None
    currency: str
    lines: list[InvoiceLine] = Field(min_length=1)
    subtotal: Decimal
    cgst_total: Decimal = Decimal(0)
    sgst_total: Decimal = Decimal(0)
    igst_total: Decimal = Decimal(0)
    freight: Decimal = Decimal(0)
    other_charges: Decimal = Decimal(0)
    round_off: Decimal = Decimal(0)
    grand_total: Decimal


class POLine(_B):
    line_ref: str
    description: str
    hsn: Optional[str] = None
    qty: Decimal = Field(ge=0)
    unit: str
    unit_price: Decimal = Field(ge=0)
    gst_rate: Optional[Decimal] = None


class PO(_B):
    po_number: str
    po_date: Optional[date] = None
    supplier_name: str
    supplier_gstin: str
    currency: str
    authorized_freight: Decimal = Decimal(0)   # freight/other charges the PO allows
    lines: list[POLine] = Field(min_length=1)


class GRNLine(_B):
    po_line_ref: str
    description: Optional[str] = None
    hsn: Optional[str] = None
    received_qty: Decimal = Field(ge=0)
    accepted_qty: Decimal = Field(ge=0)
    unit: str


class GRN(_B):
    grn_number: str
    po_number: str
    grn_date: Optional[date] = None
    lines: list[GRNLine] = Field(min_length=1)


# ---------- reconciliation results (deterministic engine output) ----------
class ExceptionItem(_B):
    code: str
    severity: str            # CRITICAL / HIGH / MEDIUM / LOW / INFO
    category: str
    line_no: Optional[int] = None
    description: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    difference: Optional[str] = None


class Candidate(_B):
    line_ref: str
    description: str
    score: float


class LineResult(_B):
    line_no: int
    description: str
    match_status: str = "N/A"    # MATCHED / OVERRIDDEN / AMBIGUOUS / NO_MATCH / N/A
    po_line_ref: Optional[str] = None
    candidates: list[Candidate] = []
    inv_qty: Optional[Decimal] = None
    po_qty: Optional[Decimal] = None
    grn_accepted: Optional[Decimal] = None
    not_received: Optional[Decimal] = None
    inv_price: Optional[Decimal] = None
    po_price: Optional[Decimal] = None
    price_var_pct: Optional[Decimal] = None
    flags: list[str] = []


class TaxRow(_B):
    line_no: int
    component: str
    taxable: Decimal
    rate: Decimal
    declared: Decimal
    calculated: Decimal
    difference: Decimal
    ok: bool


class TotalRow(_B):
    item: str
    declared: Decimal
    calculated: Decimal
    difference: Decimal
    tolerance: Decimal
    ok: bool


class CheckRow(_B):
    check: str
    expected: str
    actual: str
    ok: bool


class ReconResult(_B):
    status: str
    explanation: str
    interstate: Optional[bool] = None
    exceptions: list[ExceptionItem] = []
    lines: list[LineResult] = []
    tax_rows: list[TaxRow] = []
    total_rows: list[TotalRow] = []
    doc_checks: list[CheckRow] = []
