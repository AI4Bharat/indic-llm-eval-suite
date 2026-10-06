"""The one interface every stage uses to talk to a model.

A stage builds a list of :class:`LLMRequest` objects and calls
:func:`run_requests`.  Whether those go through the Gemini batch API or through
LiteLLM with a thread pool is decided entirely by ``inference.mode`` in the
config -- the stages never know the difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import InferenceConfig
from .costs import empty_usage


@dataclass
class LLMRequest:
    key: str                                  # unique, stable, routes the result back to a row
    prompt: str
    meta: dict = field(default_factory=dict)  # carried through untouched, never sent to the model


@dataclass
class LLMResult:
    key: str
    text: str | None = None
    error: str | None = None
    usage: dict = field(default_factory=empty_usage)
    finish_reason: str | None = None
    model_version: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text and self.text.strip())


def run_requests(
    requests: list[LLMRequest],
    model: str,
    inference: InferenceConfig,
    work_dir: Path,
    tag: str,
    pricing_overrides: dict | None = None,
    vertex=None,
) -> list[LLMResult]:
    """Execute ``requests`` and return one result per request, in request order.

    ``work_dir``/``tag`` scope the on-disk artefacts (batch input/output files and
    job state), which is what makes a batch run resumable after a crash.
    ``vertex`` carries the project / bucket / service-account settings both
    backends authenticate with.
    """
    if not requests:
        return []

    keys = [r.key for r in requests]
    if len(set(keys)) != len(keys):
        raise ValueError("run_requests: request keys must be unique")

    if inference.mode == "batch":
        from .vertex_batch import run_batch

        results = run_batch(requests, model, inference, work_dir, tag, pricing_overrides, vertex)
    else:
        from .parallel_infer import run_parallel

        results = run_parallel(requests, model, inference, pricing_overrides, vertex)

    by_key = {r.key: r for r in results}
    return [
        by_key.get(r.key, LLMResult(key=r.key, error="no result returned by backend"))
        for r in requests
    ]


def result_to_row(request: LLMRequest, result: LLMResult, attempt: int) -> dict:
    """Flatten a request/result pair for ``results.jsonl`` (the full audit trail)."""
    return {
        "key": result.key,
        "attempt": attempt,
        "ok": result.ok,
        "error": result.error,
        "finish_reason": result.finish_reason,
        "model_version": result.model_version,
        "raw_response": result.text,
        "usage": result.usage,
        "meta": request.meta,
    }
