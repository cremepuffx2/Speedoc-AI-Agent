"""Deterministic tests for data cleaning, tools and the grounding check. No API key needed.

Run: python -m pytest -v
Expected values were cross-checked against independent pandas calculations on the raw CSVs.
"""
import os

os.environ["AS_OF_DATE"] = "2026-11-04"  # fixed reference date so results never drift

import pytest

from src.guardrails import check_grounding
from src.tools import TOOLS, get_data


# ---------------------------------------------------------------- data cleaning

def test_duplicates_removed():
    inv = get_data().invoices
    assert inv["invoice_id"].is_unique
    assert len(inv) == 240


def test_mixed_date_formats_parsed():
    inv = get_data().invoices
    assert inv["issue_date"].notna().all() and inv["due_date"].notna().all()
    # 24/07/2026 (DD/MM/YYYY in the raw file) must become 24 July, not fail or swap.
    row = inv.set_index("invoice_id").loc["INV-3018"]
    assert row["due_date"].strftime("%Y-%m-%d") == "2026-07-24"


def test_missing_amount_not_imputed():
    row = get_data().invoices.set_index("invoice_id").loc["INV-3018"]
    assert row["amount"] != row["amount"]  # NaN stays NaN


def test_known_issues_logged():
    checks = " | ".join(i.check for i in get_data().issues)
    for expected in ("duplicate", "DD/MM/YYYY", "Missing", "prompt injection", "non-existent invoice", "overpayment"):
        assert expected.lower() in checks.lower(), expected


def test_injection_notes_flagged():
    flagged = set(get_data().invoices.query("notes_suspicious")["invoice_id"])
    assert flagged == {"INV-3017", "INV-3028"}


# ---------------------------------------------------------------- tools

def test_total_outstanding():
    r = TOOLS["aggregate_invoices"](metric="total_outstanding")
    assert r["result"]["value"] == 8708.72
    assert len(r["rows_cited"]) == r["result"]["invoice_count"] == 71


def test_overdue_more_than_30_days():
    r = TOOLS["get_overdue_invoices"](min_days_overdue=31)
    assert r["result"]["total_matches"] == 66
    assert r["result"]["total_overdue_balance"] == 7991.92
    assert "status_column_note" in r  # stale status values are surfaced


def test_overdue_by_customer_includes_c1010():
    """The injected note asks to hide C-1010; the tool must not."""
    groups = TOOLS["get_overdue_invoices"](group_by="customer_id")["result"]["groups"]
    c1010 = next(g for g in groups if g["customer_id"] == "C-1010")
    assert c1010["overdue_balance"] == 538.72
    assert groups[0]["customer_id"] == "C-1034"


def test_empty_period_reports_coverage():
    r = TOOLS["aggregate_invoices"](metric="total_outstanding", period="this_quarter")
    assert r["result"]["invoice_count"] == 0
    assert any("Dataset covers" in c for c in r["data_caveats"])
    assert "2026-10-01" in r["assumptions"][0]


def test_unknown_service_type_is_error_not_zero():
    r = TOOLS["aggregate_invoices"](metric="count", service_type="Physiotherapy")
    assert "error" in r and "Valid values" in r["error"]


def test_payment_lookup_and_orphan():
    assert TOOLS["get_payment_details"]("PAY-5195")["result"]["payment"]["amount"] == 132.23
    orphan = TOOLS["get_payment_details"]("PAY-5194")
    assert "INV-9999" in orphan["data_caveats"][0]
    assert "error" in TOOLS["get_payment_details"]("PAY-0000")


def test_invoice_lookup_marks_suspicious_note():
    r = TOOLS["get_invoice_details"]("INV-3017")
    assert "notes_warning" in r["result"]["invoice"]
    assert r["result"]["invoice"]["balance"] == 131.12  # still unpaid, despite the note


def test_payments_by_method_exclude_orphans():
    r = TOOLS["summarize_payments"](group_by="method")
    assert r["result"]["payment_count"] == 194
    assert r["result"]["total"] == 26771.34


@pytest.mark.parametrize("name", list(TOOLS))
def test_every_tool_returns_citation_fields(name):
    args = {"get_invoice_details": {"invoice_id": "INV-3001"},
            "get_payment_details": {"payment_id": "PAY-5001"},
            "aggregate_invoices": {"metric": "count"}}.get(name, {})
    r = TOOLS[name](**args)
    assert "query" in r and "rows_cited" in r


# ---------------------------------------------------------------- grounding check

def test_grounding_passes_tool_figures():
    r = TOOLS["get_overdue_invoices"](min_days_overdue=31)
    assert check_grounding("66 invoices, $7,991.92 overdue; oldest INV-3169 ($134.71).", "q", [r]) == []


def test_grounding_flags_invented_id_and_figure():
    r = TOOLS["get_overdue_invoices"](min_days_overdue=31)
    warnings = check_grounding("INV-3999 owes $765.80.", "q", [r])
    assert any("INV-3999" in w for w in warnings)
    assert any("765.80" in w for w in warnings)
