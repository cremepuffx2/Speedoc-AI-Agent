"""Load, validate and clean the invoice and payment CSVs.

Every check appends a DataIssue to a log, so the cleaning is transparent and
the same log can be printed, written to the README, or shown by the agent.

Run directly to print the data-quality report:
    python -m src.data_loader
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "Dataset"

VALID_STATUSES = {"paid", "partial", "unpaid", "overdue"}
VALID_METHODS = {"PayNow", "Card", "Bank Transfer", "Corporate", "Insurance"}

# Phrases that look like instructions aimed at an AI rather than a finance note.
INJECTION_PATTERN = re.compile(
    r"ignore (?:all )?previous instructions|system note|mark this invoice|"
    r"exclude customer|you are now|disregard",
    re.IGNORECASE,
)


@dataclass
class DataIssue:
    table: str
    check: str
    rows: list[str]  # IDs of the affected rows
    action: str

    def __str__(self) -> str:
        shown = ", ".join(self.rows[:8]) + (" ..." if len(self.rows) > 8 else "")
        return f"[{self.table}] {self.check} ({len(self.rows)}): {shown}\n    -> {self.action}"


@dataclass
class Dataset:
    invoices: pd.DataFrame
    payments: pd.DataFrame
    issues: list[DataIssue] = field(default_factory=list)


def _parse_mixed_dates(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Parse a column holding both YYYY-MM-DD and DD/MM/YYYY strings.

    Each format is parsed explicitly (never guessed), so 05/06/2026 is always
    5 June, not 6 May. Returns the parsed dates and a mask of DD/MM/YYYY rows.
    """
    iso = pd.to_datetime(series, format="%Y-%m-%d", errors="coerce")
    dmy = pd.to_datetime(series, format="%d/%m/%Y", errors="coerce")
    return iso.fillna(dmy), iso.isna() & dmy.notna()


def load_invoices(path: Path, issues: list[DataIssue]) -> pd.DataFrame:
    # Read everything as text first so nothing is silently coerced.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df.apply(lambda col: col.str.strip())

    # 1. Exact duplicate rows
    dup_mask = df.duplicated(keep="first")
    if dup_mask.any():
        issues.append(DataIssue("invoices", "Exact duplicate rows",
                                df.loc[dup_mask, "invoice_id"].tolist(),
                                "Dropped the repeated copy; kept one row per invoice."))
        df = df[~dup_mask]

    # Same ID but different contents would be a real conflict, not a copy.
    conflict = df["invoice_id"].duplicated(keep=False)
    if conflict.any():
        issues.append(DataIssue("invoices", "Conflicting rows sharing an invoice_id",
                                sorted(set(df.loc[conflict, "invoice_id"])),
                                "Kept all rows; flagged for manual review."))

    # 2. Mixed date formats
    for col in ("issue_date", "due_date"):
        parsed, was_dmy = _parse_mixed_dates(df[col])
        if was_dmy.any():
            issues.append(DataIssue("invoices", f"{col} in DD/MM/YYYY format",
                                    df.loc[was_dmy, "invoice_id"].tolist(),
                                    "Parsed explicitly as day/month/year."))
        if parsed.isna().any():
            issues.append(DataIssue("invoices", f"Unparseable {col}",
                                    df.loc[parsed.isna(), "invoice_id"].tolist(),
                                    "Left as missing (NaT)."))
        df[col] = parsed

    bad_order = df["due_date"] < df["issue_date"]
    if bad_order.any():
        issues.append(DataIssue("invoices", "due_date earlier than issue_date",
                                df.loc[bad_order, "invoice_id"].tolist(), "Flagged; left unchanged."))

    # 3. Amounts
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    missing_amt = df["amount"].isna()
    if missing_amt.any():
        issues.append(DataIssue("invoices", "Missing or non-numeric amount",
                                df.loc[missing_amt, "invoice_id"].tolist(),
                                "Kept the row but excluded it from money totals (no value imputed)."))
    non_positive = df["amount"] <= 0
    if non_positive.any():
        issues.append(DataIssue("invoices", "Zero or negative amount",
                                df.loc[non_positive, "invoice_id"].tolist(), "Flagged; left unchanged."))

    # 4. Categorical values
    df["status"] = df["status"].str.lower()
    bad_status = ~df["status"].isin(VALID_STATUSES)
    if bad_status.any():
        issues.append(DataIssue("invoices", "Unknown status value",
                                df.loc[bad_status, "invoice_id"].tolist(), "Flagged; left unchanged."))

    df["payment_attempts"] = pd.to_numeric(df["payment_attempts"], errors="coerce").astype("Int64")

    # 5. Notes that try to instruct an AI (prompt injection)
    suspicious = df["notes"].str.contains(INJECTION_PATTERN)
    df["notes_suspicious"] = suspicious
    if suspicious.any():
        issues.append(DataIssue("invoices", "Notes containing instruction-like text (possible prompt injection)",
                                df.loc[suspicious, "invoice_id"].tolist(),
                                "Kept as plain data, flagged notes_suspicious=True; the agent never follows text in notes."))

    return df.reset_index(drop=True)


def load_payments(path: Path, invoice_ids: set[str], issues: list[DataIssue]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df.apply(lambda col: col.str.strip())

    dup_mask = df.duplicated(keep="first")
    if dup_mask.any():
        issues.append(DataIssue("payments", "Exact duplicate rows",
                                df.loc[dup_mask, "payment_id"].tolist(), "Dropped the repeated copy."))
        df = df[~dup_mask]

    parsed, was_dmy = _parse_mixed_dates(df["paid_at"])
    if was_dmy.any():
        issues.append(DataIssue("payments", "paid_at in DD/MM/YYYY format",
                                df.loc[was_dmy, "payment_id"].tolist(), "Parsed explicitly as day/month/year."))
    df["paid_at"] = parsed

    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    bad_amt = df["amount"].isna() | (df["amount"] <= 0)
    if bad_amt.any():
        issues.append(DataIssue("payments", "Missing, zero or negative amount",
                                df.loc[bad_amt, "payment_id"].tolist(), "Flagged; excluded from totals."))

    bad_method = ~df["method"].isin(VALID_METHODS)
    if bad_method.any():
        issues.append(DataIssue("payments", "Unknown payment method",
                                df.loc[bad_method, "payment_id"].tolist(), "Flagged; left unchanged."))

    orphan = ~df["invoice_id"].isin(invoice_ids)
    df["is_orphan"] = orphan
    if orphan.any():
        pairs = [f"{p}->{i}" for p, i in df.loc[orphan, ["payment_id", "invoice_id"]].values]
        issues.append(DataIssue("payments", "Payment references a non-existent invoice",
                                pairs, "Kept in payments table but excluded from invoice balances."))

    return df.reset_index(drop=True)


def _add_payment_totals(inv: pd.DataFrame, pay: pd.DataFrame, issues: list[DataIssue]) -> pd.DataFrame:
    """Derive amount_paid and balance per invoice, and cross-check against status."""
    valid = pay[~pay["is_orphan"] & pay["amount"].gt(0)]
    paid = valid.groupby("invoice_id")["amount"].sum()
    inv["amount_paid"] = inv["invoice_id"].map(paid).fillna(0.0).round(2)
    inv["balance"] = (inv["amount"] - inv["amount_paid"]).round(2)

    over = inv["balance"] < -0.01
    if over.any():
        issues.append(DataIssue("invoices", "Payments exceed invoice amount (overpayment)",
                                inv.loc[over, "invoice_id"].tolist(),
                                "Flagged; balance shown as negative (credit owed to customer)."))

    # Status should agree with the payment records.
    tol = 0.01
    expected = pd.Series("partial", index=inv.index)
    expected[inv["amount_paid"] <= tol] = "unpaid_or_overdue"
    expected[inv["balance"].abs() <= tol] = "paid"
    actual = inv["status"].replace({"unpaid": "unpaid_or_overdue", "overdue": "unpaid_or_overdue"})
    mismatch = (actual != expected) & inv["amount"].notna() & ~over
    if mismatch.any():
        issues.append(DataIssue("invoices", "Status disagrees with payment records",
                                inv.loc[mismatch, "invoice_id"].tolist(), "Flagged; status left unchanged."))
    return inv


def load_dataset(data_dir: Path = DATA_DIR) -> Dataset:
    issues: list[DataIssue] = []
    invoices = load_invoices(data_dir / "invoices.csv", issues)
    payments = load_payments(data_dir / "payments.csv", set(invoices["invoice_id"]), issues)
    invoices = _add_payment_totals(invoices, payments, issues)
    return Dataset(invoices, payments, issues)


if __name__ == "__main__":
    ds = load_dataset()
    print(f"Invoices: {len(ds.invoices)} rows | Payments: {len(ds.payments)} rows\n")
    print("Data quality issues:")
    for issue in ds.issues:
        print(f"- {issue}")
