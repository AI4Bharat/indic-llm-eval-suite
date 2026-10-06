"""The retry loop shared by every stage.

All three stages do the same thing: turn work items into prompts, run them,
parse and validate the output, and re-run whatever failed.  Only the prompt
building and the validation differ, so that is all a stage has to supply.

Retries run as *phases*.  The primary phase repeats in the stage's configured
inference mode -- a failed batch request is retried as a new, smaller batch,
never silently downgraded to interactive calls, because that would quietly
double the price of a bad run.  If the config enables a fallback, whatever is
still failing after the primary attempts runs through a second phase in a
different mode.  That is a deliberate, opt-in choice, not a silent one: batch
rejections caused by a project-level throttle never clear no matter how many
times the batch is resubmitted, and finishing the remainder at full interactive
price is sometimes worth it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config, InferenceConfig, StageConfig
from .jsonl import append_jsonl
from .llm import LLMRequest, LLMResult, result_to_row, run_requests


@dataclass
class Phase:
    """One run of attempts under a single inference mode."""
    label: str                    # "attempt" | "fallback", used for tags and logs
    model: str                    # logical model name, as written in the config
    provider_model: str           # what is actually sent to the provider
    inference: InferenceConfig
    attempts: int


@dataclass
class Attempted:
    """One work item after its final attempt."""
    request: LLMRequest
    result: LLMResult
    payload: dict | None          # validated, stage-specific output; None when failed
    error: str | None
    attempts: int
    mode: str = "batch"           # inference mode that produced this result
    model: str = ""               # logical model name
    provider_model: str = ""      # model id actually sent


@dataclass
class Outcome:
    succeeded: list[Attempted] = field(default_factory=list)
    failed: list[Attempted] = field(default_factory=list)
    # one entry per attempt actually sent, including attempts that were retried.
    # Cost must be summed over these -- a retried request was paid for twice.
    usage_rows: list[dict] = field(default_factory=list)

    @property
    def all(self) -> list[Attempted]:
        return self.succeeded + self.failed


Validator = Callable[[LLMRequest, str], tuple[dict | None, str | None]]


def build_phases(stage: StageConfig) -> list[Phase]:
    """The primary attempts, plus a fallback phase when the config enables one."""
    phases = [Phase(
        label="attempt",
        model=stage.model,
        provider_model=stage.resolved_model(),
        inference=stage.inference,
        attempts=stage.max_attempts,
    )]
    fb = stage.fallback
    if fb.enabled and fb.max_attempts > 0:
        phases.append(Phase(
            label="fallback",
            model=fb.logical_model(stage.model),
            provider_model=fb.provider_model(stage.model),
            inference=fb.inference,
            attempts=fb.max_attempts,
        ))
    return phases


def run_with_retries(
    requests: list[LLMRequest],
    cfg: Config,
    stage: StageConfig,
    work_dir: Path,
    validate: Validator | None = None,
) -> Outcome:
    """Run ``requests``, retrying failures across the stage's phases.

    ``validate`` turns raw response text into the stage's structured payload, or
    returns an error string that sends the item back into the retry pool.
    Every request and every raw result is appended to JSONL in ``work_dir`` as
    it happens, so a run that dies halfway still leaves a usable audit trail.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    phases = build_phases(stage)
    total_attempts = sum(p.attempts for p in phases)

    pending = list(requests)
    outcome = Outcome()
    attempt = 0

    for phase_index, phase in enumerate(phases):
        if not pending:
            break
        if phase.label == "fallback":
            print(
                f"  [{stage.name}] {len(pending)} request(s) still failing after "
                f"{attempt} {phases[0].inference.mode} attempt(s); falling back to "
                f"{phase.inference.mode} mode on {phase.provider_model}"
            )

        for phase_attempt in range(1, phase.attempts + 1):
            if not pending:
                break
            attempt += 1
            last_attempt = (phase_index == len(phases) - 1) and (phase_attempt == phase.attempts)
            tag = f"{phase.label}{phase_attempt}"
            if attempt > 1:
                print(f"  [{stage.name}] {tag} ({attempt}/{total_attempts}): {len(pending)} request(s)")

            # keys must stay unique across attempts so batch outputs never collide
            attempt_requests = [
                LLMRequest(key=f"{r.key}--a{attempt}", prompt=r.prompt, meta=r.meta) for r in pending
            ]
            append_jsonl(
                work_dir / "requests.jsonl",
                [{"key": r.key, "attempt": attempt, "phase": phase.label, "mode": phase.inference.mode,
                  "model": phase.provider_model, "prompt": r.prompt, "meta": r.meta}
                 for r in attempt_requests],
            )

            results = run_requests(
                attempt_requests, phase.provider_model, phase.inference, work_dir, tag,
                cfg.pricing, cfg.vertex,
            )
            append_jsonl(
                work_dir / "results.jsonl",
                [{**result_to_row(req, res, attempt), "phase": phase.label, "mode": phase.inference.mode}
                 for req, res in zip(attempt_requests, results)],
            )

            still_pending: list[LLMRequest] = []
            for original, request, result in zip(pending, attempt_requests, results):
                outcome.usage_rows.append({
                    "language": request.meta.get("language"),
                    "attempt": attempt,
                    "phase": phase.label,
                    "mode": phase.inference.mode,
                    "ok": result.ok,
                    "usage": result.usage,
                })
                if not result.ok:
                    error = result.error or "empty response"
                    payload = None
                elif validate is None:
                    payload, error = {"text": result.text}, None
                else:
                    payload, error = validate(request, result.text)

                attempted = Attempted(
                    request, result, payload, error, attempt,
                    mode=phase.inference.mode, model=phase.model, provider_model=phase.provider_model,
                )
                if error is None:
                    attempted.payload, attempted.error = payload, None
                    outcome.succeeded.append(attempted)
                elif not last_attempt:
                    still_pending.append(original)
                else:
                    attempted.payload = None
                    outcome.failed.append(attempted)

            n_failed = len(still_pending) + sum(1 for a in outcome.failed if a.attempts == attempt)
            print(f"  [{stage.name}] {tag}: {len(pending) - n_failed} ok, {n_failed} failed")
            pending = still_pending

    return outcome
