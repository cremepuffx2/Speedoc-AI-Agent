"""
JSON-schema descriptions of each tool, sent to the LLM so it knows what it can call.
Enums restrict the LLM to values the tools accept; descriptions tell it when to use each tool.
"""

PERIODS = ["this_month", "last_month", "this_quarter", "last_quarter", "this_year"]

_FILTERS = {
    "service_type": {"type": "string", "description": "Exact service type, e.g. 'Nurse Visit'. Call get_dataset_info for valid values."},
    "customer_id": {"type": "string", "description": "Customer ID, e.g. 'C-1050'."},
    "status": {"type": "string", "enum": ["paid", "partial", "unpaid", "overdue"],
               "description": "Status as recorded in the invoice file (may be stale; for overdue use get_overdue_invoices)."},
    "period": {"type": "string", "enum": PERIODS,
               "description": "Named period relative to the as-of date. Use instead of computing dates yourself."},
    "date_from": {"type": "string", "description": "Inclusive start date, YYYY-MM-DD."},
    "date_to": {"type": "string", "description": "Inclusive end date, YYYY-MM-DD."},
    "date_field": {"type": "string", "enum": ["issue_date", "due_date"],
                   "description": "Which date the period/date range filters on. Default issue_date."},
}

TOOL_SPECS = [
    {
        "name": "get_dataset_info",
        "description": "Schema, valid values, date coverage, as-of date, what is NOT in the data, and known "
                       "data-quality issues. Call this when unsure whether the data can answer a question.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_invoices",
        "description": "List individual invoices matching filters, with amount, amount_paid and balance.",
        "parameters": {"type": "object", "properties": {
            **_FILTERS,
            "min_amount": {"type": "number", "description": "Only invoices with amount >= this."},
            "outstanding_only": {"type": "boolean", "description": "Only invoices with a remaining balance > 0."},
            "sort_by": {"type": "string", "enum": ["issue_date", "due_date", "amount", "balance"]},
            "descending": {"type": "boolean"},
        }},
    },
    {
        "name": "aggregate_invoices",
        "description": "Count, sum or average over invoices, optionally grouped. Use for totals, "
                       "breakdowns and 'how much/how many' questions.",
        "parameters": {"type": "object", "required": ["metric"], "properties": {
            "metric": {"type": "string",
                       "enum": ["count", "total_amount", "total_paid", "total_outstanding", "average_amount"]},
            "group_by": {"type": "string", "enum": ["service_type", "status", "customer_id", "issue_month"]},
            **_FILTERS,
        }},
    },
    {
        "name": "get_overdue_invoices",
        "description": "Invoices past their due date that still have a balance, with days overdue. "
                       "Always use this for overdue questions instead of filtering on status. "
                       "Use group_by for per-customer or per-service overdue totals.",
        "parameters": {"type": "object", "properties": {
            "min_days_overdue": {"type": "integer", "description": "Minimum days past due date. Default 1."},
            "service_type": _FILTERS["service_type"],
            "customer_id": _FILTERS["customer_id"],
            "group_by": {"type": "string", "enum": ["customer_id", "service_type"],
                         "description": "Return overdue totals per group instead of individual invoices."},
        }},
    },
    {
        "name": "get_invoice_details",
        "description": "Full record of one invoice: fields, notes and its payments.",
        "parameters": {"type": "object", "required": ["invoice_id"], "properties": {
            "invoice_id": {"type": "string", "description": "e.g. 'INV-3017'"},
        }},
    },
    {
        "name": "get_payment_details",
        "description": "Full record of one payment by payment_id (e.g. 'PAY-5195'), including whether "
                       "the invoice it references exists.",
        "parameters": {"type": "object", "required": ["payment_id"], "properties": {
            "payment_id": {"type": "string", "description": "e.g. 'PAY-5195'"},
        }},
    },
    {
        "name": "summarize_payments",
        "description": "Totals and counts of payments received, filtered on payment date (paid_at), "
                       "optionally grouped by method or month.",
        "parameters": {"type": "object", "properties": {
            "group_by": {"type": "string", "enum": ["method", "paid_month"]},
            "method": {"type": "string", "enum": ["PayNow", "Card", "Bank Transfer", "Corporate", "Insurance"]},
            "period": _FILTERS["period"],
            "date_from": _FILTERS["date_from"],
            "date_to": _FILTERS["date_to"],
        }},
    },
]
