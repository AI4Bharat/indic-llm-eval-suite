"""Token accounting and USD cost estimation.

Every request carries a ``usage`` dict::

    {"input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
     "cost_usd", "cost_source"}

``cost_source`` is "litellm" when the LiteLLM cost map was used, "price_table"
when we priced it ourselves, or "unknown" when the model isn't priced.

Prices are USD per 1M tokens.  Anything in the config's ``pricing:`` block wins
over the built-in table, so a new or self-hosted model only needs a config entry.
Gemini batch jobs are billed at 50% of interactive rates.
"""

from __future__ import annotations

from typing import Any

BATCH_DISCOUNT = 0.5

# USD per 1M tokens. Longest matching prefix wins, so version suffixes
# ("gemini-2.5-flash-001") resolve to their family entry.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":         {"input_per_1m": 1.25, "output_per_1m": 10.00},
    "gemini-2.5-flash-lite":  {"input_per_1m": 0.10, "output_per_1m": 0.40},
    "gemini-2.5-flash":       {"input_per_1m": 0.30, "output_per_1m": 2.50},
    "gemini-2.0-flash-lite":  {"input_per_1m": 0.075, "output_per_1m": 0.30},
    "gemini-2.0-flash":       {"input_per_1m": 0.10, "output_per_1m": 0.40},
}

USAGE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


def empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "unknown",
    }


def _candidates(model: str) -> list[str]:
    """The model id with provider prefixes progressively stripped.

    ``hosted_vllm/google/gemma-3-27b-it`` yields that, then
    ``google/gemma-3-27b-it``, then ``gemma-3-27b-it`` -- so a price keyed by any
    of those spellings is found.
    """
    name = model.strip()
    out = [name]
    while "/" in name:
        name = name.split("/", 1)[1]
        out.append(name)
    return out


def lookup_price(model: str, pricing_overrides: dict[str, dict] | None = None) -> dict[str, float] | None:
    """Find a price entry for ``model``; exact match first, then longest prefix."""
    table = {**DEFAULT_PRICING, **(pricing_overrides or {})}
    candidates = _candidates(model)
    for candidate in candidates:
        if candidate in table:
            return table[candidate]
    matches = [key for key in table if candidates[-1].startswith(key)]
    if matches:
        return table[max(matches, key=len)]
    return None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    batch: bool = False,
    pricing_overrides: dict[str, dict] | None = None,
) -> tuple[float, str]:
    """Price a request from the table. Thinking tokens are billed as output."""
    price = lookup_price(model, pricing_overrides)
    if price is None:
        return 0.0, "unknown"
    billed_output = output_tokens + reasoning_tokens
    cost = (
        input_tokens * price["input_per_1m"] + billed_output * price["output_per_1m"]
    ) / 1_000_000
    if batch:
        cost *= price.get("batch_discount", BATCH_DISCOUNT)
    return cost, "price_table"


def add_usage(target: dict, usage: dict | None) -> dict:
    """Accumulate one usage dict into a running total."""
    if not usage:
        return target
    for key in USAGE_KEYS:
        target[key] = target.get(key, 0) + int(usage.get(key) or 0)
    target["cost_usd"] = round(target.get("cost_usd", 0.0) + float(usage.get("cost_usd") or 0.0), 8)
    target["requests"] = target.get("requests", 0) + 1
    return target


def summarise(records: list[dict], group_keys: tuple[str, ...] = ("language",)) -> dict:
    """Aggregate usage across records, overall and grouped by ``group_keys``.

    Records are anything with a ``usage`` dict -- raw results, translation
    records, judge records.
    """
    total: dict[str, Any] = {"requests": 0, **{k: 0 for k in USAGE_KEYS}, "cost_usd": 0.0}
    groups: dict[str, dict] = {}
    for record in records:
        usage = record.get("usage")
        add_usage(total, usage)
        key = " | ".join(str(record.get(k, "")) for k in group_keys)
        add_usage(groups.setdefault(key, {"requests": 0, **{k: 0 for k in USAGE_KEYS}, "cost_usd": 0.0}), usage)
    return {"total": total, "by": {"keys": list(group_keys), "groups": groups}}


def merge_totals(totals: list[dict]) -> dict:
    """Sum a list of ``summarise()[...]['total']`` dicts."""
    out: dict[str, Any] = {"requests": 0, **{k: 0 for k in USAGE_KEYS}, "cost_usd": 0.0}
    for total in totals:
        for key in ("requests", *USAGE_KEYS):
            out[key] += int(total.get(key) or 0)
        out["cost_usd"] = round(out["cost_usd"] + float(total.get("cost_usd") or 0.0), 8)
    return out


def format_total(total: dict) -> str:
    return (
        f"{total.get('requests', 0):>7} req  "
        f"in {total.get('input_tokens', 0):>12,}  "
        f"out {total.get('output_tokens', 0):>12,}  "
        f"think {total.get('reasoning_tokens', 0):>11,}  "
        f"${total.get('cost_usd', 0.0):.4f}"
    )
