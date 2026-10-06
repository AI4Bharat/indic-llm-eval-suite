"""Parsing and validation of model output.

Two output formats are supported per stage:

``json``      the model returns a JSON object keyed by field name (default).
``sections``  the model returns ``### field`` blocks -- the format used by the
              older prompts in ``sovereign-llm-evals``.

Validation is derived from the *source* values, so no per-benchmark schema is
needed: a translation must contain every requested field, keep the same JSON
type, and keep list lengths identical.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Anchored: only a fence that wraps the *whole* response is a wrapper.  An
# unanchored search truncates any translation that legitimately contains a code
# fence -- which most SWE-bench issue statements and many MBPP tasks do.
_FENCE = re.compile(r"\A```(?:json|JSON)?[ \t]*\r?\n?(.*?)\r?\n?```\s*\Z", re.DOTALL)
# C0 controls that never legitimately appear in benchmark text.  A model that
# writes "\boxed" instead of "\\boxed" inside JSON emits U+0008 + "oxed": valid
# JSON, silently destroyed LaTeX.  Newline/CR/tab are excluded because sources
# genuinely contain them (Asymptote blocks, code listings).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SECTION = re.compile(r"^###(?!#)\s*(.+)$", re.MULTILINE)


class ParseError(ValueError):
    """Raised when model output cannot be turned into a dict."""


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def strip_fences(text: str) -> str:
    """Remove a code fence that encloses the entire string, if there is one."""
    match = _FENCE.match(text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and prose."""
    if not text or not text.strip():
        raise ParseError("empty response")
    # Try the response as-is first: a model that correctly returned raw JSON
    # containing a fenced code block must not have that block stripped out.
    obj, error = None, None
    for candidate in (text.strip(), strip_fences(text)):
        try:
            obj = json.loads(candidate)
            break
        except json.JSONDecodeError as e:
            error = e
    else:
        # last resort: the outermost {...} span
        candidate = strip_fences(text)
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ParseError(f"no JSON object found in response: {candidate[:200]!r}") from None
        try:
            obj = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as e:
            raise ParseError(f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ParseError(f"expected a JSON object, got {type(obj).__name__}")
    return obj


def parse_sections(text: str) -> dict:
    """Parse ``### key`` / value blocks into a dict."""
    if not text or not text.strip():
        raise ParseError("empty response")
    matches = list(_SECTION.finditer(text))
    if not matches:
        raise ParseError(f"no '### key' sections found in response: {text[:200]!r}")
    out: dict[str, Any] = {}
    for i, match in enumerate(matches):
        key = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        if value.startswith("```"):
            value = strip_fences(value)
        out[key] = value
    return out


def parse_response(text: str, output_format: str) -> dict:
    return parse_json(text) if output_format == "json" else parse_sections(text)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def coerce_like(value: Any, reference: Any) -> Any:
    """Best-effort coercion of a parsed value into the shape of the source value.

    Handles the common case where a model returns a JSON-encoded list as a
    string (``'["a", "b"]'``) when the source field was a list, or a bare number
    where the source was text.  A genuine shape mismatch (a list where a string
    belongs) is left alone so validation rejects it -- silently stringifying it
    would write ``'["oops"]'`` into the benchmark.
    """
    if isinstance(reference, str):
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return json.dumps(value, ensure_ascii=False)
        return value
    if isinstance(reference, list) and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, list) else value
    return value


def control_char_error(name: str, value: Any, src: Any) -> str | None:
    """Reject text carrying control characters the source does not have.

    Catches the JSON-escape failure mode: an undoubled ``\\b``/``\\f``/``\\t`` inside a
    LaTeX command decodes to a control character, so ``\\boxed`` silently becomes
    backspace + "oxed".  Rejecting it here costs one free retry; letting it through
    ships corrupted LaTeX that no downstream stage can see.
    """
    text = value if isinstance(value, str) else "".join(v for v in value if isinstance(v, str)) \
        if isinstance(value, list) else ""
    if not text:
        return None
    found = _CONTROL.findall(text)
    if found:
        return (f"field '{name}': control character U+{ord(found[0]):04X} in the translation "
                f"(an undoubled JSON escape -- '\\{{cmd}}' written instead of '\\\\{{cmd}}')")
    source_text = src if isinstance(src, str) else "".join(v for v in src if isinstance(v, str)) \
        if isinstance(src, list) else ""
    if text.count("\t") > source_text.count("\t"):
        return (f"field '{name}': {text.count(chr(9))} tab(s) against {source_text.count(chr(9))} "
                f"in the source (likely an undoubled '\\t' LaTeX command)")
    return None


def validate_translation(parsed: dict, source: dict[str, Any]) -> tuple[dict, str | None]:
    """Check a parsed translation against the source group.

    Returns ``(cleaned, error)``; ``error`` is None when the translation is usable.
    """
    cleaned: dict[str, Any] = {}
    for name, src in source.items():
        if name not in parsed:
            return {}, f"missing field '{name}' (got keys: {sorted(parsed)})"
        value = coerce_like(parsed[name], src)

        if isinstance(src, str):
            if not isinstance(value, str):
                return {}, f"field '{name}': expected a string, got {type(value).__name__}"
            if src.strip() and not value.strip():
                return {}, f"field '{name}': empty translation"
        elif isinstance(src, list):
            if not isinstance(value, list):
                return {}, f"field '{name}': expected a list, got {type(value).__name__}"
            if len(value) != len(src):
                return {}, f"field '{name}': expected {len(src)} items, got {len(value)}"
            if any(isinstance(v, str) and not v.strip() for v in value):
                return {}, f"field '{name}': list contains an empty item"
        elif isinstance(src, dict):
            if not isinstance(value, dict):
                return {}, f"field '{name}': expected an object, got {type(value).__name__}"
            missing = sorted(set(src) - set(value))
            if missing:
                return {}, f"field '{name}': missing keys {missing}"

        error = control_char_error(name, value, src)
        if error:
            return {}, error

        cleaned[name] = value
    return cleaned, None


# --------------------------------------------------------------------------- #
# judge output
# --------------------------------------------------------------------------- #

_TRUE = {"pass", "passed", "true", "yes", "ok", "accept", "accepted", "correct"}
_FALSE = {"fail", "failed", "false", "no", "reject", "rejected", "incorrect"}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return None


def validate_judgement(parsed: dict, pass_threshold: float, score_scale: float = 10.0) -> tuple[dict, str | None]:
    """Normalise a judge response to ``{score, passed, feedback}``.

    Accepts a few common key spellings so judge prompts don't have to agree on
    one exact vocabulary.
    """
    score = None
    for key in ("score", "quality_score", "rating", "translation_score", "overall_score"):
        if key in parsed:
            try:
                score = float(parsed[key])
            except (TypeError, ValueError):
                return {}, f"field '{key}': not a number ({parsed[key]!r})"
            break
    if score is None:
        return {}, f"missing a score field (got keys: {sorted(parsed)})"
    # A judge answering on the wrong scale (8 when the rubric is 0-100) would
    # quietly send every translation to the corrector, so reject what cannot fit.
    if score_scale and not (0 <= score <= score_scale):
        return {}, f"score {score} is outside the configured 0-{score_scale:g} scale"

    passed = None
    for key in ("pass", "passed", "verdict", "result", "status"):
        if key in parsed:
            passed = _as_bool(parsed[key])
            if passed is not None:
                break
    if passed is None:
        passed = score >= pass_threshold

    feedback = ""
    for key in ("feedback", "reasoning", "explanation", "reason", "summary",
                "analysis_cot", "analysis", "comments", "critique", "error_analysis", "issues"):
        if key in parsed and parsed[key]:
            feedback = parsed[key] if isinstance(parsed[key], str) else json.dumps(parsed[key], ensure_ascii=False)
            break

    if not passed and not feedback:
        return {}, "judge failed the translation but gave no feedback"

    return {"score": score, "passed": bool(passed), "feedback": feedback}, None
