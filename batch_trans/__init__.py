"""A config-driven pipeline for translating LLM benchmarks into many languages.

Stages (each independently runnable, each file-in / file-out):

  1. translate  source benchmark            -> translate/**/records.jsonl
  2. judge      translation records         -> judge_r<N>/**/records.jsonl
  3. correct    judge records               -> correct_r<N>/**/records.jsonl
  4. assemble   records + original rows     -> final/**/<split>.jsonl (or the Hub)

Inference runs either through the Gemini batch API (~50% cheaper, the default)
or through LiteLLM with a thread pool (any provider, including local models).
"""

__version__ = "0.1.0"
