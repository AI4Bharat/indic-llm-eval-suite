"""Unit tests for the parts that are easy to get subtly wrong.

    python -m tests.test_units
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_trans import vertex_batch
from batch_trans.config import InferenceConfig
from batch_trans.costs import estimate_cost, lookup_price, summarise
from batch_trans.fields import field_key, get_path, missing_fields, source_for_group
from batch_trans.llm import LLMRequest
from batch_trans.parsing import ParseError, parse_json, parse_sections, validate_judgement, validate_translation
from batch_trans.prompts import Prompt
from batch_trans.records import output_skeleton, safe_key

FAILURES: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}{(' -- ' + detail) if detail else ''}")
        FAILURES.append(label)


def raises(label, fn, exc=ParseError):
    try:
        fn()
    except exc:
        check(label, True)
        return
    check(label, False, "no exception raised")


def test_parsing():
    print("\n== parsing ==")
    check("plain JSON", parse_json('{"a": 1}') == {"a": 1})
    check("fenced JSON", parse_json('```json\n{"a": "b"}\n```') == {"a": "b"})
    check("JSON with prose around it",
          parse_json('Sure, here you go:\n{"a": "b"}\nHope that helps!') == {"a": "b"})
    check("unicode preserved", parse_json('{"q": "नमस्ते"}')["q"] == "नमस्ते")
    raises("empty response rejected", lambda: parse_json("   "))
    raises("prose-only response rejected", lambda: parse_json("I cannot translate this."))
    raises("JSON array rejected", lambda: parse_json("[1, 2, 3]"))
    sections = parse_sections("### question\nप्रश्न\n### answer\nउत्तर")
    check("section format", sections == {"question": "प्रश्न", "answer": "उत्तर"})


def test_validation():
    print("\n== translation validation ==")
    source = {"question": "Q?", "choices": ["a", "b", "c"]}
    ok, err = validate_translation({"question": "प्र?", "choices": ["क", "ख", "ग"]}, source)
    check("well-formed translation accepted", err is None and ok["choices"] == ["क", "ख", "ग"])

    _, err = validate_translation({"question": "प्र?"}, source)
    check("missing field rejected", err is not None and "choices" in err, str(err))

    _, err = validate_translation({"question": "प्र?", "choices": ["क", "ख"]}, source)
    check("dropped option rejected", err is not None and "3 items" in err, str(err))

    _, err = validate_translation({"question": "", "choices": ["क", "ख", "ग"]}, source)
    check("empty translation rejected", err is not None and "empty" in err, str(err))

    _, err = validate_translation({"question": "प्र?", "choices": ["क", "  ", "ग"]}, source)
    check("empty option rejected", err is not None, str(err))

    ok, err = validate_translation({"question": "प्र?", "choices": '["क", "ख", "ग"]'}, source)
    check("stringified list coerced back to a list", err is None and ok["choices"] == ["क", "ख", "ग"], str(err))

    _, err = validate_translation({"question": ["oops"], "choices": ["क", "ख", "ग"]}, source)
    check("wrong type rejected", err is not None and "string" in err, str(err))


def test_judgement():
    print("\n== judgement validation ==")
    j, err = validate_judgement({"score": 9, "pass": True, "feedback": "fine"}, 7)
    check("standard judgement", err is None and j["score"] == 9.0 and j["passed"] is True)

    j, err = validate_judgement({"score": 4, "verdict": "FAIL", "explanation": "wrong number"}, 7)
    check("alternative key spellings", err is None and j["passed"] is False and j["feedback"] == "wrong number")

    j, err = validate_judgement({"score": 3, "feedback": "bad"}, 7)
    check("pass inferred from threshold when absent", err is None and j["passed"] is False)

    _, err = validate_judgement({"feedback": "bad"}, 7)
    check("missing score rejected", err is not None and "score" in err)

    _, err = validate_judgement({"score": "eight", "feedback": "x"}, 7)
    check("non-numeric score rejected", err is not None)

    _, err = validate_judgement({"score": 2, "pass": False}, 7)
    check("failure without feedback rejected", err is not None, "corrector needs feedback to work from")


def test_prompts():
    print("\n== prompt rendering ==")
    prompt = Prompt(path="x", sha256="y", text=(
        'Translate to {target_language}:\n{question}\n'
        'Return {"score": 5, "pass": true} shaped output. Unknown: {nope}'
    ))
    rendered = prompt.render({"target_language": "Hindi", "question": "What is 2+2?"})
    check("placeholders substituted", "Hindi" in rendered and "What is 2+2?" in rendered)
    check("JSON examples in the prompt survive", '{"score": 5, "pass": true}' in rendered)
    check("unknown placeholder left alone", "{nope}" in rendered)
    check("unfilled placeholders reported",
          prompt.missing_placeholders({"target_language": "Hindi"}) == ["nope", "question"])

    rendered = prompt.render({"target_language": "Hindi", "question": ["a", "b"]})
    check("list values rendered as JSON", '["a", "b"]' in rendered)

    skeleton = json.loads(output_skeleton({"question": "Q", "choices": ["a", "b"]}))
    check("output skeleton mirrors source shape",
          isinstance(skeleton["choices"], list) and len(skeleton["choices"]) == 2)


def test_fields():
    print("\n== field selection ==")
    row = {"question": "Q?", "choices": {"text": ["a", "b"], "label": ["A", "B"]}}
    check("dotted path read", get_path(row, "choices.text") == ["a", "b"])
    check("dotted path flattened to a key", field_key("choices.text") == "choices_text")
    check("group extracted",
          source_for_group(row, ["question", "choices.text"]) == {"question": "Q?", "choices_text": ["a", "b"]})
    check("missing field detected", missing_fields(row, ["question", "answer"]) == ["answer"])
    check("keys stay unique and sorted", safe_key(7, "Hindi", "q1", "g0").startswith("000007-Hindi-q1-g0"))
    check("unsafe characters stripped from keys", "/" not in safe_key(1, "a/b c", "d:e"))


def test_batch_encoding():
    print("\n== vertex batch encoding ==")
    inference = InferenceConfig(mode="batch", temperature=0.3, max_output_tokens=1024,
                                json_mode=True, thinking_budget=0)
    gen = vertex_batch.build_generation_config(inference)
    check("json mode requested", gen["responseMimeType"] == "application/json")
    check("thinking disabled explicitly", gen["thinkingConfig"]["thinkingBudget"] == 0)

    line = vertex_batch.encode_request(LLMRequest(key="k1", prompt="hello"), gen)
    check("batch line carries only 'request'", list(line) == ["request"])
    check("prompt in the request", line["request"]["contents"][0]["parts"][0]["text"] == "hello")
    check("generation config attached", line["request"]["generationConfig"]["temperature"] == 0.3)
    check("batch line is JSON serialisable", isinstance(json.dumps(line), str))


def _prediction(prompt, text, **response_extra):
    """A Vertex prediction line: the echoed request plus the response."""
    return {
        "status": "",
        "request": {"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
        "response": {
            "candidates": [{"content": {"parts": [{"text": text}], "role": "model"},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
            **response_extra,
        },
    }


def test_batch_matching():
    print("\n== vertex batch result matching ==")
    requests = [LLMRequest(key=k, prompt=p) for k, p in
                [("a", "translate X"), ("b", "translate Y"), ("c", "translate X")]]
    # Vertex returns no key and does not preserve order
    lines = [_prediction("translate Y", "Y-hi"),
             _prediction("translate X", "X-hi-1"),
             _prediction("translate X", "X-hi-2")]
    results = {r.key: r.text for r in
               vertex_batch.match_results(lines, requests, "gemini-2.5-flash", None)}
    check("out-of-order response matched by prompt", results["b"] == "Y-hi")
    check("all requests resolved", set(results) == {"a", "b", "c"})
    check("duplicate prompts each get a response",
          {results["a"], results["c"]} == {"X-hi-1", "X-hi-2"})

    stray = vertex_batch.match_results([_prediction("something else", "?")], requests[:1],
                                       "gemini-2.5-flash", None)
    check("unmatchable line dropped rather than misrouted", stray == [])

    failed = vertex_batch.match_results(
        [{"status": "Quota exceeded", "request": {"contents": [{"parts": [{"text": "translate X"}]}]}}],
        [requests[0]], "gemini-2.5-flash", None)
    check("non-empty status becomes a failed result",
          len(failed) == 1 and not failed[0].ok and "Quota" in failed[0].error)


def test_batch_decoding():
    print("\n== vertex batch decoding ==")
    payload = {
        "response": {
            "candidates": [{
                "content": {"parts": [{"text": "thinking...", "thought": True},
                                      {"text": '{"question": "प्र?"}'}], "role": "model"},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200,
                              "thoughtsTokenCount": 300, "totalTokenCount": 1500},
            "modelVersion": "gemini-2.5-flash",
        },
    }
    result = vertex_batch.decode_line(payload, "k1", "gemini-2.5-flash", None)
    check("answer text extracted", result.ok and result.text == '{"question": "प्र?"}')
    check("thought parts excluded from the answer", "thinking..." not in (result.text or ""))
    check("token counts read", result.usage["input_tokens"] == 1000
          and result.usage["output_tokens"] == 200 and result.usage["reasoning_tokens"] == 300)
    # batch price = half of (1000 in @ $0.30/M + 500 billed out @ $2.50/M)
    expected = (1000 * 0.30 + 500 * 2.50) / 1_000_000 * 0.5
    check("batch discount applied", abs(result.usage["cost_usd"] - expected) < 1e-12,
          f"{result.usage['cost_usd']} != {expected}")

    err = vertex_batch.decode_line({"error": {"code": 429, "message": "quota"}}, "k2",
                                   "gemini-2.5-flash", None)
    check("error line becomes a failed result", not err.ok and "quota" in err.error)

    blocked = vertex_batch.decode_line(
        {"response": {"candidates": [{"finishReason": "SAFETY", "content": {}}]}}, "k3",
        "gemini-2.5-flash", None)
    check("blocked response becomes a failed result", not blocked.ok and "SAFETY" in blocked.error)

    snake = vertex_batch.decode_line(
        {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                      "usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 5}}}, "k4",
        "gemini-2.5-flash", None)
    check("snake_case usage keys also read", snake.usage["input_tokens"] == 10)


def test_costs():
    print("\n== costs ==")
    check("versioned model resolves to its family",
          lookup_price("gemini-2.5-flash-001") == lookup_price("gemini-2.5-flash"))
    check("litellm-style prefix stripped",
          lookup_price("gemini/gemini-2.5-pro") == lookup_price("gemini-2.5-pro"))
    check("flash-lite is not matched by flash",
          lookup_price("gemini-2.5-flash-lite")["output_per_1m"] == 0.40)

    interactive, _ = estimate_cost("gemini-2.5-flash", 1_000_000, 0, batch=False)
    batched, _ = estimate_cost("gemini-2.5-flash", 1_000_000, 0, batch=True)
    check("batch mode costs half", abs(batched * 2 - interactive) < 1e-12)

    _, source = estimate_cost("some-local-model", 100, 100)
    check("unknown model reported as unpriced", source == "unknown")

    custom, source = estimate_cost("my-model", 1_000_000, 0,
                                   pricing_overrides={"my-model": {"input_per_1m": 2.0, "output_per_1m": 4.0}})
    check("config pricing override honoured", abs(custom - 2.0) < 1e-12 and source == "price_table")

    usage = {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 1, "total_tokens": 16, "cost_usd": 0.5}
    report = summarise([{"language": "Hindi", "usage": usage}, {"language": "Tamil", "usage": usage},
                        {"language": "Hindi", "usage": usage}], group_keys=("language",))
    check("totals summed", report["total"]["requests"] == 3 and report["total"]["input_tokens"] == 30)
    check("grouped by language", report["by"]["groups"]["Hindi"]["requests"] == 2)


def main() -> int:
    for test in (test_parsing, test_validation, test_judgement, test_prompts,
                 test_fields, test_batch_encoding, test_batch_matching, test_batch_decoding,
                 test_costs):
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}\n")
        return 1
    print("All unit checks passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
