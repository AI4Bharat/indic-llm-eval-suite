"""Parallel (non-batch) inference backend, via LiteLLM.

LiteLLM means the same code path serves Vertex AI, OpenAI, Anthropic, vLLM,
Ollama, or anything else with an OpenAI-compatible endpoint -- set
``inference.litellm_model`` (and ``api_base`` for self-hosted models) and the
rest of the pipeline is unchanged.

For ``vertex_ai/*`` models the project, location and service-account key from
the config's ``vertex:`` block are passed through automatically, so parallel
mode authenticates exactly like batch mode.

Use this mode for local models, for quick debugging runs, and for providers
without a batch endpoint.  Batch mode is roughly half the price on Gemini, so
prefer it for full benchmark sweeps.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from tqdm import tqdm

from .config import InferenceConfig
from .costs import estimate_cost
from .llm import LLMRequest, LLMResult

_configured = threading.Lock()
_did_configure = False


def _configure_litellm() -> Any:
    global _did_configure
    import litellm

    with _configured:
        if not _did_configure:
            litellm.drop_params = True          # ignore params a given provider doesn't support
            litellm.suppress_debug_info = True
            _did_configure = True
    return litellm


def build_call_kwargs(model: str, inference: InferenceConfig, vertex=None) -> dict:
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": inference.temperature,
        "num_retries": inference.num_retries,
        "timeout": inference.request_timeout,
    }
    if inference.max_output_tokens:
        kwargs["max_tokens"] = inference.max_output_tokens
    if inference.json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if inference.api_base:
        kwargs["api_base"] = inference.api_base
    if inference.api_key_env:
        key = os.environ.get(inference.api_key_env)
        if not key:
            raise RuntimeError(f"inference.api_key_env={inference.api_key_env} is not set in the environment")
        kwargs["api_key"] = key
    if inference.thinking_budget is not None:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": inference.thinking_budget}
    if model.startswith("vertex_ai/") and vertex is not None:
        if vertex.project:
            kwargs["vertex_project"] = vertex.project
        if vertex.location:
            kwargs["vertex_location"] = vertex.location
        if vertex.credentials_file:
            kwargs["vertex_credentials"] = vertex.credentials_file
    kwargs.update(inference.extra_params)
    return kwargs


def _usage_from_response(response: Any, model: str, pricing_overrides: dict | None) -> dict:
    litellm = _configure_litellm()
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
    # LiteLLM reports reasoning tokens inside completion_tokens; keep them separate
    # in our accounting but don't double-count them.
    output_tokens = max(output_tokens - reasoning_tokens, 0)

    cost_usd, cost_source = None, "unknown"
    try:
        cost_usd = float(litellm.completion_cost(completion_response=response))
        cost_source = "litellm"
    except Exception:
        cost_usd = None
    if not cost_usd:
        cost_usd, cost_source = estimate_cost(
            model, input_tokens, output_tokens, reasoning_tokens,
            batch=False, pricing_overrides=pricing_overrides,
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens),
        "cost_usd": round(cost_usd, 8),
        "cost_source": cost_source,
    }


def _call_one(request: LLMRequest, call_kwargs: dict, model: str, pricing_overrides: dict | None) -> LLMResult:
    litellm = _configure_litellm()
    try:
        response = litellm.completion(
            messages=[{"role": "user", "content": request.prompt}],
            **call_kwargs,
        )
    except Exception as e:
        return LLMResult(key=request.key, error=f"{type(e).__name__}: {e}")

    choice = response.choices[0]
    text = getattr(choice.message, "content", None)
    finish_reason = getattr(choice, "finish_reason", None)
    usage = _usage_from_response(response, model, pricing_overrides)
    error = None if (text and text.strip()) else f"empty response (finish_reason={finish_reason})"
    return LLMResult(
        key=request.key,
        text=text,
        error=error,
        usage=usage,
        finish_reason=finish_reason,
        model_version=getattr(response, "model", None),
    )


def run_parallel(
    requests: list[LLMRequest],
    model: str,
    inference: InferenceConfig,
    pricing_overrides: dict | None = None,
    vertex=None,
) -> list[LLMResult]:
    call_kwargs = build_call_kwargs(model, inference, vertex)
    print(f"  [parallel] {len(requests)} requests via {model} with {inference.num_workers} workers")

    results: list[LLMResult] = []
    with ThreadPoolExecutor(max_workers=inference.num_workers) as executor:
        futures = {
            executor.submit(_call_one, request, call_kwargs, model, pricing_overrides): request
            for request in requests
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="  inference", leave=False):
            request = futures[future]
            try:
                results.append(future.result())
            except Exception as e:  # a worker crash must not lose the whole run
                results.append(LLMResult(key=request.key, error=f"worker crashed: {type(e).__name__}: {e}"))
    return results
