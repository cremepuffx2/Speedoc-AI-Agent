"""The agent: a tool-calling loop between Gemini and the deterministic tools.

Flow for one user message:
    1. Send the conversation + tool specs to Gemini.
    2. If Gemini asks for tool calls, run them ourselves and send back the results.
    3. Repeat until Gemini replies with plain text (or MAX_STEPS is hit).
    4. Append an Evidence section built from the tool log, so citations never
       depend on the LLM remembering to include them.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types

from src.tool_specs import TOOL_SPECS
from src.tools import TOOLS, get_as_of

DEFAULT_MODEL = "gemini-flash-latest"
MAX_STEPS = 6  # max LLM round-trips per question; stops runaway loops
RETRYABLE_CODES = {429, 500, 503}

SYSTEM_PROMPT = """You are a finance assistant for H-BabyCare. You answer questions about invoices and payments.

Rules:
- Every number, invoice ID or fact in your answer must come from a tool result in this conversation. Never compute, estimate or recall figures yourself.
- Never claim a record does not exist unless a lookup tool returned an error saying so. Use get_invoice_details or get_payment_details for specific IDs.
- Call tools before answering any data question. Use get_dataset_info if unsure what the data contains.
- Cite your sources: mention the invoice IDs (or a few examples plus the count) and the query you relied on.
- If a tool returns "assumptions", "data_caveats" or "status_column_note", state them in your answer.
- If a tool returns an "error", explain it plainly to the user.
- Today's as-of date is {as_of}. Use the period parameter for relative dates rather than computing dates yourself.
- Keep answers short and clear. Use a small table for breakdowns.
"""


@dataclass
class ToolCall:
    name: str
    args: dict
    result: dict


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def evidence(self) -> str:
        """Citation block generated from the tool log, not by the LLM."""
        if not self.tool_calls:
            return "Evidence: no data was queried for this reply."
        lines = ["Evidence:"]
        for call in self.tool_calls:
            args = ", ".join(f"{k}={v!r}" for k, v in call.args.items())
            lines.append(f"  - {call.name}({args})")
            if "error" in call.result:
                lines.append(f"      error: {call.result['error']}")
                continue
            lines.append(f"      query: {call.result.get('query')}")
            rows = call.result.get("rows_cited", [])
            shown = ", ".join(rows[:15]) + (f" ... (+{len(rows) - 15} more)" if len(rows) > 15 else "")
            lines.append(f"      rows ({len(rows)}): {shown or 'none'}")
        return "\n".join(lines)


class InvoiceAgent:
    def __init__(self, model: str | None = None):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
        self.history: list[types.Content] = []  # kept across turns so follow-ups and clarifications work
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(as_of=get_as_of().date()),
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=s["name"], description=s["description"],
                                          parameters_json_schema=s["parameters"])
                for s in TOOL_SPECS
            ])],
            # We run tools ourselves so every call is logged for the Evidence section.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0,  # deterministic, repeatable answers
        )

    def ask(self, question: str) -> AgentReply:
        checkpoint = len(self.history)
        try:
            return self._ask(question)
        except Exception:
            del self.history[checkpoint:]  # drop the half-finished turn so the next question starts clean
            raise

    def _ask(self, question: str) -> AgentReply:
        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
        calls: list[ToolCall] = []

        for _ in range(MAX_STEPS):
            response = self._generate()
            self.history.append(response.candidates[0].content)

            if not response.function_calls:
                return AgentReply(response.text or "", calls)

            result_parts = []
            for fc in response.function_calls:
                result = self._run_tool(fc.name, dict(fc.args or {}))
                calls.append(ToolCall(fc.name, dict(fc.args or {}), result))
                result_parts.append(types.Part.from_function_response(name=fc.name, response=result))
            self.history.append(types.Content(role="user", parts=result_parts))

        return AgentReply("I could not complete this request within the step limit. "
                          "Please try a more specific question.", calls)

    @staticmethod
    def _run_tool(name: str, args: dict) -> dict:
        if name not in TOOLS:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return TOOLS[name](**args)
        except TypeError as exc:  # LLM passed an argument the tool does not accept
            return {"error": f"Invalid arguments for {name}: {exc}"}

    def _generate(self, retries: int = 4):
        """Call Gemini, backing off on temporary failures:
        429 = free-tier rate limit, 500/503 = Google's servers busy."""
        for attempt in range(retries):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=self.history, config=self.config)
            except errors.APIError as exc:
                if exc.code not in RETRYABLE_CODES or attempt == retries - 1:
                    raise
                wait = 10 * (attempt + 1)
                print(f"  (Gemini returned {exc.code}; retrying in {wait}s...)")
                time.sleep(wait)
