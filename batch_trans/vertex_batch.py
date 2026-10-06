"""Vertex AI batch prediction backend: GCS in, one job per chunk, GCS out.

Flow per call:

1. Write the requests as a Vertex batch JSONL (chunked, since a job has a
   request cap) and upload each chunk to GCS.
2. Create one batch prediction job per chunk, pointing at its input URI and an
   output prefix.
3. Poll every job until all reach a terminal state.
4. List and download the ``predictions.jsonl`` files each job wrote, and match
   the responses back to our requests.

Job names, URIs and downloaded files are recorded in ``state.json`` next to the
local input files, so re-running a stage after a crash -- or after simply
walking away for the several hours a large job takes -- picks the existing jobs
back up instead of paying for them twice.

**Matching results back to rows.** Vertex batch output has no request id field;
what it does carry is the echoed ``request``. So each response is matched to a
request by hashing the prompt text that came back. Requests that share
byte-identical prompts are handed responses in submission order -- which is safe,
because an identical prompt is satisfied equally well by either response.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .config import InferenceConfig
from .costs import estimate_cost
from .llm import LLMRequest, LLMResult
from .vertex import VertexConfig, download, genai_client, list_uris, upload

TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
}
SUCCESS_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}


# --------------------------------------------------------------------------- #
# request encoding
# --------------------------------------------------------------------------- #

def build_generation_config(inference: InferenceConfig) -> dict:
    generation_config: dict[str, Any] = {"temperature": inference.temperature}
    if inference.max_output_tokens:
        generation_config["maxOutputTokens"] = inference.max_output_tokens
    if inference.json_mode:
        generation_config["responseMimeType"] = "application/json"
    if inference.thinking_budget is not None:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": inference.thinking_budget,
            "includeThoughts": False,
        }
    return generation_config


def encode_request(request: LLMRequest, generation_config: dict) -> dict:
    """One line of a Vertex batch input file.

    Only ``request`` is written: Vertex validates the line against
    GenerateContentRequest and does not carry arbitrary extra fields through to
    the output, so the routing key lives in the prompt hash instead.
    """
    return {
        "request": {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation_config,
        }
    }


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------- #
# response decoding
# --------------------------------------------------------------------------- #

def _get(mapping: dict, *names: str, default=None):
    """Read a key that may be camelCase or snake_case depending on the transport."""
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) is not None:
            return mapping[name]
    return default


def echoed_prompt(line: dict) -> str | None:
    """The prompt text Vertex echoed back, used to route the response to a row."""
    request = _get(line, "request", default={}) or {}
    contents = _get(request, "contents", default=[]) or []
    texts = [
        part.get("text", "")
        for content in contents
        for part in (_get(content, "parts", default=[]) or [])
        if isinstance(part, dict)
    ]
    return "".join(texts) or None


def extract_text(response: dict) -> tuple[str | None, str | None]:
    """Pull the answer text and finish reason out of a GenerateContentResponse."""
    candidates = _get(response, "candidates", default=[]) or []
    if not candidates:
        return None, "NO_CANDIDATES"
    candidate = candidates[0]
    finish_reason = _get(candidate, "finishReason", "finish_reason")
    content = _get(candidate, "content", default={}) or {}
    parts = _get(content, "parts", default=[]) or []
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and part.get("text") and not part.get("thought")
    ]
    return ("".join(texts) if texts else None), finish_reason


def extract_usage(response: dict, model: str, pricing_overrides: dict | None) -> dict:
    meta = _get(response, "usageMetadata", "usage_metadata", default={}) or {}
    input_tokens = int(_get(meta, "promptTokenCount", "prompt_token_count", default=0) or 0)
    output_tokens = int(_get(meta, "candidatesTokenCount", "candidates_token_count", default=0) or 0)
    reasoning_tokens = int(_get(meta, "thoughtsTokenCount", "thoughts_token_count", default=0) or 0)
    total_tokens = int(_get(meta, "totalTokenCount", "total_token_count", default=0) or 0)
    cost, source = estimate_cost(
        model, input_tokens, output_tokens, reasoning_tokens,
        batch=True, pricing_overrides=pricing_overrides,
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens + reasoning_tokens),
        "cost_usd": round(cost, 8),
        "cost_source": source,
    }


def decode_line(line: dict, key: str, model: str, pricing_overrides: dict | None) -> LLMResult:
    """Turn one Vertex prediction line into a result for ``key``."""
    status = line.get("status")
    if status:
        return LLMResult(key=key, error=f"batch error: {str(status)[:500]}")
    if line.get("error"):
        return LLMResult(key=key, error=f"batch error: {json.dumps(line['error'], ensure_ascii=False)[:500]}")

    response = _get(line, "response", default={}) or {}
    if not response:
        return LLMResult(key=key, error="batch line has no 'response'")

    text, finish_reason = extract_text(response)
    usage = extract_usage(response, model, pricing_overrides)
    error = None if text else f"empty response (finishReason={finish_reason})"
    return LLMResult(
        key=key,
        text=text,
        error=error,
        usage=usage,
        finish_reason=finish_reason,
        model_version=_get(response, "modelVersion", "model_version"),
    )


def match_results(
    lines: list[dict], requests: list[LLMRequest], model: str, pricing_overrides: dict | None
) -> list[LLMResult]:
    """Route prediction lines back to requests via the echoed prompt text."""
    pending: dict[str, deque[str]] = defaultdict(deque)
    for request in requests:
        pending[prompt_hash(request.prompt)].append(request.key)

    results, unmatched = [], 0
    for line in lines:
        key = line.get("key")               # honoured if a future output format carries one
        if not key:
            prompt = echoed_prompt(line)
            queue = pending.get(prompt_hash(prompt)) if prompt else None
            if not queue:
                unmatched += 1
                continue
            key = queue.popleft()
        results.append(decode_line(line, key, model, pricing_overrides))

    if unmatched:
        print(f"  [batch] {unmatched} prediction line(s) could not be matched to a request")
    return results


# --------------------------------------------------------------------------- #
# the job loop
# --------------------------------------------------------------------------- #

def run_batch(
    requests: list[LLMRequest],
    model: str,
    inference: InferenceConfig,
    work_dir: Path,
    tag: str,
    pricing_overrides: dict | None = None,
    vertex: VertexConfig | None = None,
) -> list[LLMResult]:
    vertex = vertex or VertexConfig.from_dict(None)
    vertex.require("batch mode")

    batch_dir = work_dir / "batch" / tag
    batch_dir.mkdir(parents=True, exist_ok=True)
    state_path = batch_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"chunks": {}}

    def save_state() -> None:
        state_path.write_text(json.dumps(state, indent=2))

    client = genai_client(vertex)
    generation_config = build_generation_config(inference)
    chunks = _chunks(requests, inference.max_requests_per_batch)
    # a stable, readable GCS location: <prefix>/<benchmark>/<stage>/<config>/<split>/<attempt>
    remote_base = "/".join(work_dir.resolve().parts[-4:] + (tag,))
    print(f"  [batch] {len(requests)} requests in {len(chunks)} job(s) -> {vertex.uri(remote_base)}")

    # ---- submit (or recover) one job per chunk --------------------------- #
    for index, chunk in enumerate(chunks):
        chunk_id = f"{index:03d}"
        entry = state["chunks"].setdefault(chunk_id, {})
        entry["num_requests"] = len(chunk)

        local_input = batch_dir / f"input_{chunk_id}.jsonl"
        if not local_input.exists():
            with open(local_input, "w", encoding="utf-8") as f:
                for request in chunk:
                    f.write(json.dumps(encode_request(request, generation_config), ensure_ascii=False) + "\n")
        entry["input_path"] = str(local_input)

        if entry.get("job_name"):
            print(f"  [batch] chunk {chunk_id}: resuming job {entry['job_name']}")
            continue

        input_uri = vertex.uri(remote_base, f"input_{chunk_id}.jsonl")
        output_uri = vertex.uri(remote_base, f"output_{chunk_id}")
        upload(vertex, local_input, input_uri)
        job = client.batches.create(
            model=model,
            src=input_uri,
            config={"dest": output_uri, "display_name": f"{work_dir.name}-{tag}-{chunk_id}"},
        )
        entry.update(
            input_uri=input_uri, output_uri=output_uri,
            job_name=job.name, state=_state_name(job.state),
        )
        save_state()
        print(f"  [batch] chunk {chunk_id}: submitted {job.name} ({len(chunk)} requests)")

    save_state()

    # ---- poll ------------------------------------------------------------ #
    deadline = time.time() + inference.poll_timeout_hours * 3600
    pending = [c for c, e in state["chunks"].items() if _state_name(e.get("state")) not in TERMINAL_STATES]
    while pending:
        if time.time() > deadline:
            raise TimeoutError(
                f"batch jobs still running after {inference.poll_timeout_hours}h; "
                f"state kept in {state_path} -- re-run this stage to resume polling"
            )
        time.sleep(inference.poll_interval_seconds)
        still_pending = []
        for chunk_id in pending:
            entry = state["chunks"][chunk_id]
            job = client.batches.get(name=entry["job_name"])
            entry["state"] = _state_name(job.state)
            if entry["state"] in TERMINAL_STATES:
                if job.error:
                    entry["error"] = str(job.error)
                actual = getattr(job.dest, "gcs_uri", None) if job.dest is not None else None
                if actual:
                    entry["output_uri"] = actual
                print(f"  [batch] chunk {chunk_id}: {entry['state']}")
            else:
                still_pending.append(chunk_id)
        save_state()
        if still_pending:
            states = ", ".join(
                f"{c}={state['chunks'][c]['state'].removeprefix('JOB_STATE_')}" for c in still_pending
            )
            print(f"  [batch] waiting on {len(still_pending)} job(s): {states}", flush=True)
        pending = still_pending

    # ---- collect --------------------------------------------------------- #
    results: list[LLMResult] = []
    for index, chunk in enumerate(chunks):
        chunk_id = f"{index:03d}"
        entry = state["chunks"][chunk_id]
        if entry.get("state") not in SUCCESS_STATES:
            print(f"  [batch] chunk {chunk_id} did not succeed ({entry.get('state')}): {entry.get('error')}")
            continue

        local_dir = batch_dir / f"output_{chunk_id}"
        local_files = sorted(local_dir.glob("*.jsonl")) if local_dir.exists() else []
        if not local_files:
            remote_files = list_uris(vertex, entry["output_uri"])
            if not remote_files:
                print(f"  [batch] chunk {chunk_id} succeeded but wrote no predictions to {entry['output_uri']}")
                continue
            for number, uri in enumerate(remote_files):
                local_files.append(download(vertex, uri, local_dir / f"predictions_{number:03d}.jsonl"))
            entry["output_files"] = [str(p) for p in local_files]
            save_state()
            print(f"  [batch] chunk {chunk_id}: downloaded {len(local_files)} prediction file(s)")

        lines = []
        for path in local_files:
            for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    print(f"  [batch] {path.name}:{lineno}: unparseable output line ({e})")
        results.extend(match_results(lines, chunk, model, pricing_overrides))

    return results


def _state_name(state: Any) -> str:
    """Normalise a JobState enum / string to its plain name."""
    if state is None:
        return ""
    name = getattr(state, "name", None) or str(state)
    return name.rsplit(".", 1)[-1]
