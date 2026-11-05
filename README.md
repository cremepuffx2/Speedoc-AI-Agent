# H-BabyCare Invoicing Agent

A read-only assistant that answers plain-English questions about H-BabyCare invoices and payments. Every figure
in an answer is computed by deterministic Python (pandas) tools, and every answer carries an **Evidence** section
listing the queries run and the rows used. The agent refuses questions the data cannot answer, states or asks
about ambiguous terms, ignores instructions embedded in data, and can draft (never send) payment-reminder emails.

All data in `Dataset/` is synthetic.

```
You: Which invoices are more than 30 days overdue?

Agent: 66 invoices are more than 30 days overdue as of 2026-11-04, with $7,991.92 outstanding.
       Note: 30 of these have a status column of 'unpaid'/'partial' rather than 'overdue'. ...

Evidence:
  - get_overdue_invoices(min_days_overdue=31)
      query: invoices WHERE balance > 0 AND (as_of - due_date) >= 31 days ORDER BY days_overdue DESC
      rows (66): INV-3169, INV-3174, INV-3161, ... (+63 more)
```

---

## Contents

1. [Setup](#1-setup)
2. [Usage](#2-usage)
3. [Architecture](#3-architecture)
4. [Data preparation and quality](#4-data-preparation-and-quality)
5. [Grounding and citations](#5-grounding-and-citations)
6. [Guardrails](#6-guardrails)
7. [Draft payment reminders](#7-draft-payment-reminders)
8. [Testing](#8-testing)
9. [Assumptions](#9-assumptions)
10. [Known failure modes and limitations](#10-known-failure-modes-and-limitations)

---

## 1. Setup

**Requirements:** Python 3.10+ and a Google Gemini API key (the free tier is sufficient; keys are issued at
[aistudio.google.com](https://aistudio.google.com)).

```bash
git clone https://github.com/cremepuffx2/Speedoc-AI-Agent.git
cd Speedoc-AI-Agent
pip install -r requirements.txt
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Edit `.env` and set the key:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes | – | Gemini API key |
| `GEMINI_MODEL` | No | `gemini-flash-latest` | Model name. Run `python main.py --list-models` to see models available to the key |
| `AS_OF_DATE` | No | today | Reference date (`YYYY-MM-DD`) for "overdue" and relative periods such as "this quarter" |
| `EVAL_PAUSE` | No | `6` | Seconds between evaluation prompts (raise if hitting free-tier rate limits) |

`.env` is listed in `.gitignore`. API keys must never be committed.

---

## 2. Usage

| Task | Command |
|---|---|
| Interactive chat | `python main.py` |
| List available Gemini models | `python main.py --list-models` |
| Print the data-quality report | `python -m src.data_loader` |
| Run unit tests (no API key needed) | `python -m pytest -v` |
| Run the live agent evaluation | `python -m tests.run_agent_eval` |
| Run selected evaluation cases only | `python -m tests.run_agent_eval 7 8` (overwrites the report with those cases) |

In the chat, `reset` clears the conversation and `exit` quits. Conversation history is kept between questions,
so follow-ups and answers to clarifying questions work as expected.

Example questions:

- Which invoices are more than 30 days overdue?
- What is our total outstanding by service type?
- Summarise overdue accounts by customer.
- How much have we collected by payment method in Q3?
- Tell me about INV-3001. / Tell me about payment PAY-5194.
- Draft a polite payment-reminder email for our most overdue invoice.

---

## 3. Architecture

```
                     ┌─────────────────────────────────────────────────────┐
  user question ───► │ Gemini (src/agent.py)                               │
                     │  system prompt + conversation + tool specifications │
                     └──────────────┬──────────────────────────────────────┘
                                    │ requests a tool call, e.g.
                                    │ aggregate_invoices(metric="total_outstanding", group_by="service_type")
                                    ▼
                     ┌─────────────────────────────────────────────────────┐
                     │ Tools (src/tools.py) – pandas over cleaned data     │ ◄── Dataset/*.csv
                     │ return: result + rows_cited + query + assumptions   │     via src/data_loader.py
                     └──────────────┬──────────────────────────────────────┘
                                    │ JSON result back to Gemini (loop repeats as needed)
                                    ▼
                     Gemini writes the answer from the tool results
                                    │
                                    ▼
                     Grounding check (src/guardrails.py) ── fails ──► one automatic repair attempt
                                    │ passes
                                    ▼
                     Answer + warnings (if any) + Evidence section built from the tool log
```

The LLM decides *which* tool to call and *how to phrase* the answer. It never computes figures. Tool calls are
executed by the application (automatic function calling is disabled), so every call is logged and can be cited.

### Repository layout

| Path | Purpose |
|---|---|
| `main.py` | Command-line chat interface |
| `src/data_loader.py` | Loads, validates and cleans both CSVs; produces the data-quality issue log |
| `src/tools.py` | Deterministic query tools and the `TOOLS` registry |
| `src/tool_specs.py` | JSON-schema tool descriptions sent to Gemini (enums restrict arguments to valid values) |
| `src/agent.py` | System prompt, tool-calling loop, retries, verify-then-repair, Evidence rendering |
| `src/guardrails.py` | Post-answer grounding check |
| `tests/test_tools.py` | Deterministic unit tests for cleaning, tools, grounding and reminders |
| `tests/run_agent_eval.py` | End-to-end evaluation through the live model |
| `tests/eval_results.md` | Latest evaluation report: every test prompt, answer, evidence and check result |
| `Dataset/` | Synthetic `invoices.csv` and `payments.csv` |

### Tools

| Tool | Answers | Notes |
|---|---|---|
| `get_dataset_info` | What data exists, valid values, date coverage, what is *not* in the data | Used to decide whether a question is answerable |
| `list_invoices` | Invoice listings with filters | Returns totals over all matches so the LLM never sums rows |
| `aggregate_invoices` | Counts, sums, averages, optionally grouped | Metrics: `count`, `total_amount`, `total_paid`, `total_outstanding`, `average_amount` |
| `get_overdue_invoices` | Overdue invoices, optionally grouped by customer or service | Computed from due dates and payments, not the status column |
| `get_invoice_details` | One invoice with its payments and notes | Only tool that returns notes; they are labelled untrusted |
| `get_payment_details` | One payment, including whether its invoice exists | |
| `summarize_payments` | Collections, optionally by method or month | Excludes payments to non-existent invoices |
| `draft_payment_reminder` | Draft reminder email for one overdue invoice | Draft only; see [section 7](#7-draft-payment-reminders) |

All tools are read-only. Invalid arguments (e.g. an unknown service type) return an explicit `error` listing the
valid values instead of an empty or zero result. Named periods (`this_month`, `last_month`, `this_quarter`,
`last_quarter`, `this_year`) are resolved to exact dates in code and reported back in `assumptions`.

---

## 4. Data preparation and quality

`src/data_loader.py` reads every column as text, applies explicit conversions, and records each finding as a
`DataIssue` (table, check, affected IDs, action taken). Run `python -m src.data_loader` to print the log.

The cleaning policy is conservative: fix only what is unambiguous, flag everything else, and never impute
financial values.

| Issue | Rows affected | Handling |
|---|---|---|
| Exact duplicate invoice rows | INV-3041, INV-3124 | Second copy dropped (242 → 240 invoices) |
| Mixed date formats (`YYYY-MM-DD` and `DD/MM/YYYY`) | 32 `issue_date`, 32 `due_date` values | Each format parsed explicitly, so `05/06/2026` is always 5 June |
| Missing invoice amount | INV-3018 | Kept, amount left null, excluded from money totals and from reminder drafting |
| Instruction-like text in `notes` (prompt injection) | INV-3017, INV-3028 | Kept as data, flagged `notes_suspicious`; never followed or copied into emails |
| Payment referencing a non-existent invoice | PAY-5194 → INV-9999 | Kept in payments, excluded from invoice balances and collection totals |
| Payments exceeding invoice amount | INV-3001 (two payments) | Flagged; negative balance treated as a credit, excluded from outstanding totals |
| Status column out of date | 35 invoices past due (as of 2026-11-04) recorded as `unpaid`/`partial` | Overdue is derived from `due_date` and payments; tools report the mismatch |

Derived columns: `amount_paid` (sum of valid payments per invoice) and `balance` (`amount − amount_paid`).

Additional checks run on every load and currently find nothing: conflicting rows sharing an invoice ID, due date
before issue date, non-positive amounts, unknown status or payment-method values, unparseable payment dates, and
payments dated before their invoice was issued. The last two detect a file that has been re-sorted or re-saved
incorrectly (for example, sorting a single column in Excel misaligns every row).

> **Do not open and re-save the CSVs in Excel.** Excel can re-sort columns independently and rewrite dates in
> local formats. Restore with `git restore Dataset/` if this happens.

---

## 5. Grounding and citations

Answers are grounded in three ways:

1. **Tools compute everything.** Totals, groupings, day counts and date ranges are calculated in pandas. Tools
   return pre-computed totals specifically so the LLM has no reason to add numbers itself.
2. **Every tool result is self-citing.** Each returns `rows_cited` (invoice/payment IDs used), `query` (the
   filter and aggregation in SQL-like form) and `assumptions` / `data_caveats` where relevant.
3. **The Evidence section is generated by code, not the LLM.** It is built from the log of tool calls actually
   executed, so a citation cannot be invented. If a reply relies only on results from earlier in the
   conversation, the Evidence section says so.

---

## 6. Guardrails

Guardrails are layered so that a failure in one layer is caught by the next. Prompt instructions alone are not
relied on.

| Layer | Mechanism | Prevents |
|---|---|---|
| 1. Data | Suspicious notes flagged at load time | Injected text being treated as trustworthy |
| 2. Tools | Read-only toolset; input validation with explicit errors; summary tools never return notes | Data modification (impossible by construction); silent wrong answers; injection exposure |
| 3. System prompt | Rules for grounding, unanswerable questions, ambiguity, untrusted content and scope | Guessing, unflagged assumptions, following embedded instructions |
| 4. Grounding check | Code verifies every ID and decimal figure in the answer against tool results | Hallucinated or LLM-computed figures reaching the user |

### Unanswerable questions

The prompt requires the agent to state "The data cannot answer this", name what is missing and offer the closest
answerable question. `get_dataset_info` lists what the data does not contain (patient or clinical data, contact
details, costs and margins, tax breakdowns). Unknown IDs are confirmed with a lookup tool before being reported as
missing.

### Ambiguous questions

- Where a term has several materially different meanings and no sensible default (e.g. "revenue" as invoiced vs.
  collected), the agent asks one clarifying question or presents each interpretation, clearly labelled.
- Where a sensible default exists, the agent answers and states the assumption on the first line.
- Where the literal reading returns no data (e.g. "this quarter" when no invoices were issued in it), the agent
  says so, reports the dataset's date coverage, and labels any alternative period's figures as such.

### Prompt injection

Text in data fields is treated as data. The notes on INV-3028 (asking for a customer to be excluded from overdue
summaries) and INV-3017 (asking for the invoice to be marked as paid) are reported to the user as suspicious and
have no effect on results. User-side attempts to override rules or extract the system prompt are declined.

### Grounding check and repair

After each answer, `src/guardrails.py` extracts every `INV-`/`PAY-`/`C-` ID and every decimal figure and confirms
each appears in a tool result from the session (IDs may also come from the user's question). If any value cannot
be traced:

1. The agent automatically sends the model one repair instruction listing the unverified values and asking it to
   call the appropriate tools and rewrite the answer.
2. If the rewritten answer passes, it is shown with a note in the Evidence section that it was regenerated.
3. If it still fails, the answer is shown with a **WARNING** block listing the unverified values.

This layer is necessary in practice: when asked "What's our revenue?", the model has repeatedly produced
plausible but invented totals (e.g. $107,329.80 against an actual $35,050.66) while citing a tool it had not
called. The check detects this and the repair step recovers the correct figures.

### Resilience

Temporary failures (HTTP 429/500/503 and dropped network connections) are retried with increasing back-off. A
question that fails part-way is removed from the conversation history so later questions start cleanly. The CLI
reports errors without exiting.

---

## 7. Draft payment reminders

`draft_payment_reminder(invoice_id, tone)` produces a reminder email for one overdue invoice. **Nothing is ever
sent**: no sending capability exists in the codebase.

- **Facts come from code.** The subject and body are produced from a template filled with the invoice's data
  (service, issue and due dates, balance, prior partial payments). The LLM presents the draft and may adjust tone
  on request, but the grounding check still applies to every figure.
- **Refused when inappropriate:** fully paid invoices, invoices not yet due, invoices with no recorded amount,
  and unknown IDs.
- **Tone:** `auto` (default) is gentle up to 60 days overdue and firmer (still polite, with an offer to discuss a
  payment arrangement) beyond that. `gentle` and `firm` can be requested explicitly.
- **Placeholders, not inventions.** Customer names, email addresses and payment instructions are not in the data,
  so the draft uses `[Customer name]`, `[Contact details]` and `[Payment instructions]`.
- **Notes never enter the email.** Relevant internal context (e.g. "Second reminder sent", repeated payment
  attempts, stale status) is returned separately as `review_notes` for the sender. Notes flagged as possible
  prompt injection are withheld entirely.

---

## 8. Testing

### Unit tests: `tests/test_tools.py`

Deterministic, no API key required, runs in seconds (`python -m pytest -v`). Covers duplicate removal,
mixed-date parsing, no imputation of missing amounts, the issue log, injection flagging, key totals (cross-checked
against independent pandas calculations on the raw CSVs), overdue logic, empty-period handling, input validation,
payment lookups, citation fields on every tool, grounding-check behaviour, and reminder drafting rules.

### Agent evaluation: `tests/run_agent_eval.py`

Runs 19 prompts through the live model and writes `tests/eval_results.md` with each prompt, full answer, Evidence
section, grounding warnings and automatic check results.

| Category | Cases | Examples |
|---|---|---|
| Core questions | 6 | Both example questions from the brief; customer drill-down; overpaid invoice; orphan payment |
| Ambiguous | 3 | "This quarter" with no data; "revenue"; two-turn clarification |
| Prompt injection | 4 | Notes asking to hide a customer and to mark an invoice paid; user asking to reveal the system prompt; request to copy an injected note into an email |
| Out of scope | 1 | Request to mark an invoice paid and email a receipt |
| Data cannot answer | 3 | Patient diagnoses and phone numbers; revenue forecast; non-existent invoice |
| Draft action | 2 | Reminder for the most overdue invoice; reminder for a paid invoice (must decline) |

Automatic checks verify tool usage, key figures, refusal wording and grounding. They are heuristics: model
wording varies between runs, so a `CHECK` result means "review the transcript", not necessarily a defect. The
report is saved after every case so an interrupted run keeps completed results. `AS_OF_DATE` is fixed at
2026-11-04 in both test suites so results are reproducible.

---

## 9. Assumptions

| Topic | Assumption |
|---|---|
| As-of date | Defaults to the current date; set `AS_OF_DATE` to reproduce results. Tests use 2026-11-04 |
| Overdue | Past `due_date` with a positive balance. Partially paid invoices count; the remaining balance is what is overdue. The `status` column is not used to decide this because it is stale for some invoices |
| "More than N days" | Strictly greater, i.e. N+1 days or more |
| Outstanding | `amount − sum of valid payments`, for invoices with a positive balance. Overpayments are credits and are not netted against other invoices |
| Periods | Calendar months and quarters relative to the as-of date. Invoice periods filter on `issue_date` by default (`due_date` on request); payment periods filter on `paid_at` |
| Revenue | Ambiguous; treated as invoiced (`total_amount`) or collected (payments), and always labelled |
| Currency | Not specified in the data; amounts are shown with `$` |
| Missing amounts | Not imputed; excluded from money totals and disclosed as a caveat |
| Orphan payments | Excluded from balances and collection totals; visible through `get_payment_details` |
| Customer details | Only `customer_id` exists; names and contact details are placeholders in drafts |

---

## 10. Known failure modes and limitations

| Area | Limitation | Mitigation / impact |
|---|---|---|
| Grounding check scope | Only IDs and decimal figures are verified. Whole numbers (counts, days, years) are not, as they appear too often in dates and prose to check reliably | Prompt rules and tool-provided counts; an incorrect count would not be flagged |
| Grounding check semantics | Confirms a figure exists in a tool result, not that it is described correctly (e.g. a real total labelled with the wrong meaning) | Evidence section lets the reader confirm which query produced each figure |
| Repair limit | One automatic repair attempt per answer | A persistently ungrounded answer is shown with a WARNING rather than blocked |
| Model variability | Wording, tool choice and whether the model asks or assumes on ambiguous questions vary between runs and models (smaller "lite" models vary more) | `temperature=0`; evaluation transcripts should be reviewed, not only check results |
| Evaluation checks | Keyword-based; can produce false positives (e.g. matching a phrase inside a negation) or false negatives on rephrasings | Checks are scoped narrowly (e.g. email body only) and each case documents its purpose |
| Injection detection | Suspicious notes are detected with a phrase list; novel phrasing may not be flagged at load time | Notes are treated as untrusted regardless of the flag, summary tools never return notes, and no write tools exist |
| Question coverage | Only questions expressible through the eight tools can be answered (e.g. no payment-timeliness analysis per customer, no ageing buckets) | The agent states when a question is outside what the tools support; new tools can be added in `src/tools.py` and `src/tool_specs.py` |
| Large result sets | Tools return at most 50 rows to the model; totals always cover all matches | Answers give counts and examples rather than full listings |
| Data dependency | Results are only as current as the CSVs; there is no live connection to a finance system | Date coverage is reported when a query falls outside it |
| External service | Free-tier rate limits, model overloads and network drops can delay or fail requests | Automatic retries with back-off; clean error messages; evaluation pause configurable via `EVAL_PAUSE` |
| Model availability | Model names change over time; `gemini-flash-latest` is an alias whose target may change | Override with `GEMINI_MODEL`; list options with `python main.py --list-models` |
