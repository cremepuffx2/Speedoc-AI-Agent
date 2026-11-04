"""
Post-answer grounding check.

The system prompt asks the LLM to use only tool results, but prompts are requests,
not guarantees. This module verifies the final answer in code: every record ID and
every money figure in the answer must appear in this turn's tool results (or, for IDs,
in the user's question). Anything else is reported as unverified.
"""
from __future__ import annotations

import json
import re

ID_PATTERN = re.compile(r"\b(?:INV|PAY|C)-\d+\b", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{1,2}\b|\d+\.\d{1,2}\b")


def _collect(value, ids: set[str], numbers: set[float]) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _collect(v, ids, numbers)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, ids, numbers)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        numbers.add(round(float(value), 2))
    elif isinstance(value, str):
        ids.update(m.upper() for m in ID_PATTERN.findall(value))
        numbers.update(round(float(m.replace(",", "")), 2) for m in MONEY_PATTERN.findall(value))


def check_grounding(answer: str, question: str, tool_results: list[dict]) -> list[str]:
    known_ids: set[str] = {m.upper() for m in ID_PATTERN.findall(question)}
    known_numbers: set[float] = set()
    for result in tool_results:
        _collect(json.loads(json.dumps(result, default=str)), known_ids, known_numbers)

    answer_ids = {m.upper() for m in ID_PATTERN.findall(answer)}
    unknown_ids = sorted(answer_ids - known_ids)

    unknown_numbers = sorted({
        m for m in MONEY_PATTERN.findall(answer)
        if not any(abs(float(m.replace(",", "")) - n) < 0.01 for n in known_numbers)
    })

    warnings = []
    if unknown_ids:
        warnings.append(f"IDs not found in any tool result: {', '.join(unknown_ids)}")
    if unknown_numbers:
        warnings.append(f"Figures not found in any tool result (possibly computed by the LLM): "
                        f"{', '.join(unknown_numbers)}")
    return warnings
