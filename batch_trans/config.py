"""Config loading and schema for the benchmark translation pipeline.

Everything benchmark-specific lives in a YAML file: which fields to translate,
which prompts to use, which model, which languages, where the data comes from
and where it goes.  Adding a new benchmark should mean adding a config + prompts,
never touching the pipeline code.

See ``configs/`` for worked examples and ``README.md`` for the full key reference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .vertex import VertexConfig

STAGES = ("translate", "judge", "correct")

# Writing system each language is expected to be produced in, for prompts that
# pin the output script.  Override or extend with `language_scripts:` in a config.
DEFAULT_SCRIPTS = {
    "Assamese": "Bengali-Assamese",
    "Bengali": "Bengali",
    "Bodo": "Devanagari",
    "Dogri": "Devanagari",
    "Gujarati": "Gujarati",
    "Hindi": "Devanagari",
    "Kannada": "Kannada",
    "Kashmiri": "Perso-Arabic",
    "Konkani": "Devanagari",
    "Maithili": "Devanagari",
    "Malayalam": "Malayalam",
    "Manipuri": "Meitei Mayek",
    "Marathi": "Devanagari",
    "Nepali": "Devanagari",
    "Odia": "Odia",
    "Odiya": "Odia",
    "Punjabi": "Gurmukhi",
    "Sanskrit": "Devanagari",
    "Santali": "Ol Chiki",
    "Sindhi": "Perso-Arabic",
    "Tamil": "Tamil",
    "Telugu": "Telugu",
    "Urdu": "Perso-Arabic",
    "English": "Latin",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ValueError(f"config: missing required key '{key}' in {where}")
    return mapping[key]


def as_litellm_model(model: str) -> str:
    """Provider-qualify a bare model name for LiteLLM.

    Inference here is Vertex-only, so a bare ``gemini-3.7-flash`` can only mean
    ``vertex_ai/gemini-3.7-flash``.  Anything already carrying a provider prefix
    (``hosted_vllm/...``) is left alone.
    """
    return model if "/" in model else f"vertex_ai/{model}"


def _resolve(path: str | None, base: Path) -> str | None:
    """Resolve a config-relative path (prompt files, local data) against the config dir."""
    if path is None:
        return None
    p = Path(os.path.expanduser(path))
    if p.is_absolute():
        return str(p)
    return str((base / p).resolve())


# --------------------------------------------------------------------------- #
# source
# --------------------------------------------------------------------------- #

@dataclass
class Unit:
    """One (dataset config, split) pair to process.

    ``hf_config`` is the name passed to ``load_dataset`` (None for datasets with
    no configs); ``config`` is the label used in output paths and records.
    """
    config: str
    split: str
    hf_config: str | None = None
    local_path: str | None = None

    @property
    def label(self) -> str:
        return f"{self.config}/{self.split}"


@dataclass
class SourceConfig:
    type: str = "huggingface"          # "huggingface" | "local"
    path: str = ""                      # hub repo id, unused for local
    cache_dir: str | None = None
    id_field: str = "id"                # id column; generated as a UUID when absent
    revision: str | None = None
    units: list[Unit] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict, base: Path) -> "SourceConfig":
        stype = raw.get("type", "huggingface")
        if stype not in ("huggingface", "local"):
            raise ValueError(f"config: source.type must be 'huggingface' or 'local', got {stype!r}")

        units: list[Unit] = []
        if stype == "huggingface":
            configs = raw.get("configs")
            if configs:
                # {config_name: [split, ...]}
                for cfg_name, splits in configs.items():
                    for split in splits:
                        units.append(Unit(config=str(cfg_name), split=str(split), hf_config=str(cfg_name)))
            else:
                splits = _require(raw, "splits", "source (no 'configs' given)")
                for split in splits:
                    units.append(Unit(config="default", split=str(split), hf_config=None))
        else:
            # local: files may be {split: path} or {config: {split: path}}
            files = _require(raw, "files", "source (type: local)")
            for key, value in files.items():
                if isinstance(value, dict):
                    for split, path in value.items():
                        units.append(Unit(config=str(key), split=str(split),
                                          local_path=_resolve(str(path), base)))
                else:
                    units.append(Unit(config="default", split=str(key),
                                      local_path=_resolve(str(value), base)))

        return cls(
            type=stype,
            path=str(raw.get("path", "")),
            cache_dir=_resolve(raw.get("cache_dir"), base),
            id_field=raw.get("id_field") or "id",
            revision=raw.get("revision"),
            units=units,
        )


# --------------------------------------------------------------------------- #
# inference
# --------------------------------------------------------------------------- #

@dataclass
class InferenceConfig:
    """How to actually run the requests for a stage."""
    mode: str = "batch"                     # "batch" (Gemini batch API) | "parallel" (LiteLLM)
    num_workers: int = 16                   # parallel mode only
    temperature: float = 0.2
    max_output_tokens: int | None = 8192
    json_mode: bool = True                  # ask the provider for strict JSON output
    thinking_budget: int | None = None      # Gemini thinking budget; 0 disables thinking
    # batch mode
    max_requests_per_batch: int = 50_000
    poll_interval_seconds: int = 60
    poll_timeout_hours: float = 48.0
    delete_uploaded_files: bool = False
    # parallel mode
    litellm_model: str | None = None        # defaults to the stage model
    api_base: str | None = None
    api_key_env: str | None = None
    num_retries: int = 3
    request_timeout: int = 600
    extra_params: dict = field(default_factory=dict)   # merged verbatim into the LiteLLM call

    @classmethod
    def from_dict(cls, raw: dict | None) -> "InferenceConfig":
        raw = dict(raw or {})
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"config: unknown inference keys {sorted(unknown)}")
        cfg = cls(**raw)
        if cfg.mode not in ("batch", "parallel"):
            raise ValueError(f"config: inference.mode must be 'batch' or 'parallel', got {cfg.mode!r}")
        return cfg


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #

@dataclass
class FallbackConfig:
    """What to do with requests still failing after the primary attempts.

    Batch mode can fail for reasons that have nothing to do with the request --
    a project-level throttle will reject a fixed share of every batch, however
    many times you resubmit it.  Retrying the same way forever just re-pays for
    the same rejections, so a stage can finish the remainder through a different
    inference mode instead.  Off unless a config turns it on.
    """
    enabled: bool = False
    max_attempts: int = 1
    model: str | None = None                # defaults to the stage model
    inference: InferenceConfig = field(default_factory=lambda: InferenceConfig(mode="parallel"))

    @classmethod
    def from_dict(cls, raw: dict | None, stage_inference: InferenceConfig) -> "FallbackConfig":
        raw = dict(raw or {})
        enabled = bool(raw.pop("enabled", False))
        max_attempts = int(raw.pop("max_attempts", 1))
        model = raw.pop("model", None)
        # remaining keys are inference settings, defaulting to parallel mode and
        # inheriting the stage's generation settings so only the differences
        # have to be written out
        inherited = {
            "temperature": stage_inference.temperature,
            "max_output_tokens": stage_inference.max_output_tokens,
            "json_mode": stage_inference.json_mode,
            "thinking_budget": stage_inference.thinking_budget,
        }
        inference = InferenceConfig.from_dict({"mode": "parallel", **inherited, **raw})
        return cls(enabled=enabled, max_attempts=max_attempts, model=model, inference=inference)

    def logical_model(self, stage_model: str) -> str:
        return self.model or stage_model

    def provider_model(self, stage_model: str) -> str:
        logical = self.logical_model(stage_model)
        if self.inference.mode == "parallel":
            return self.inference.litellm_model or as_litellm_model(logical)
        return logical


@dataclass
class StageConfig:
    name: str
    enabled: bool = True
    prompt_path: str = ""
    model: str = ""
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    output_format: str = "json"             # "json" | "sections"
    max_attempts: int = 3                   # retries for failed / malformed requests
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    # judge only
    pass_threshold: float = 7.0
    score_scale: float = 10.0
    # correct only: when set, judgements scoring at most this go to the
    # corrector, instead of whatever the judge itself marked as failed
    max_score: float | None = None

    @classmethod
    def from_dict(cls, name: str, raw: dict | None, base: Path, defaults: dict) -> "StageConfig":
        raw = dict(raw or {})
        merged = {**defaults, **raw}
        inference = InferenceConfig.from_dict(merged.get("inference"))
        return cls(
            name=name,
            enabled=bool(merged.get("enabled", True)),
            prompt_path=_resolve(merged.get("prompt"), base) or "",
            model=str(merged.get("model", "")),
            inference=inference,
            fallback=FallbackConfig.from_dict(merged.get("fallback"), inference),
            output_format=merged.get("output_format", "json"),
            max_attempts=int(merged.get("max_attempts", 3)),
            pass_threshold=float(merged.get("pass_threshold", 7.0)),
            score_scale=float(merged.get("score_scale", 10.0)),
            max_score=None if merged.get("max_score") is None else float(merged["max_score"]),
        )

    def resolved_model(self) -> str:
        """Model id to send to the provider for this stage's inference mode."""
        if self.inference.mode == "parallel":
            return self.inference.litellm_model or as_litellm_model(self.model)
        return self.model


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

@dataclass
class OutputConfig:
    root: str = "./data"
    destination: str = "local"              # "local" | "huggingface"
    hub_repo: str | None = None
    private: bool = True
    column_template: str = "{field}_{language}"
    keep_source_columns: bool = True
    include_quality_columns: bool = False    # add per-language stage / judge-score columns
    format: str = "jsonl"                    # final dataset dump: "jsonl" | "parquet"

    @classmethod
    def from_dict(cls, raw: dict | None, base: Path) -> "OutputConfig":
        raw = dict(raw or {})
        return cls(
            root=_resolve(raw.get("root", "./data"), base),
            destination=raw.get("destination", "local"),
            hub_repo=raw.get("hub_repo"),
            private=bool(raw.get("private", True)),
            column_template=raw.get("column_template", "{field}_{language}"),
            keep_source_columns=bool(raw.get("keep_source_columns", True)),
            include_quality_columns=bool(raw.get("include_quality_columns", False)),
            format=raw.get("format", "jsonl"),
        )


# blank columns the human reviewer fills in
HUMAN_FIELDS = ("human_verdict", "human_score", "human_issue", "human_comments", "reviewer")


@dataclass
class ReviewConfig:
    """Stage 5 -- how to build the flat human-review file.

    ``fail_threshold`` defaults to the judge's own pass threshold, so "failed
    even after the last judging round" means the same thing here as it does
    upstream.  ``sample_rate`` is the fraction of *passing* instances pulled in
    as a spot check; selection is a hash of the record id, so the same rows are
    sampled every time the stage is re-run.
    """

    fail_threshold: float | None = None      # None -> judge.pass_threshold
    sample_rate: float = 0.25
    sample_seed: str = "human-review-v1"
    csv: bool = True                         # also write a spreadsheet of the queue
    include_judge_details: bool = True       # keep judge_parsed / raw text in history
    include_all_rows: bool = True            # write the full file, not just the queue
    human_fields: list[str] = field(default_factory=lambda: list(HUMAN_FIELDS))
    # A second correction round run at a higher bar can score *worse* than the
    # first -- the corrector is rewriting an already-good translation. Both keys
    # below are off by default so a benchmark with a single correction round
    # reviews exactly as it did before they existed.
    prefer_best_translation: bool = False    # hand over the highest-scoring version, not the newest
    queue_regressions: bool = False          # queue rows whose newest version scores below their best
    # The CSV is the queue by default -- that is what a reviewer is asked to act
    # on. Turn this on to also write a spreadsheet of *every* instance, which is
    # what you want when the whole set goes out for human verification rather
    # than just the flagged tail.
    csv_all_rows: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ReviewConfig":
        raw = dict(raw or {})
        threshold = raw.get("fail_threshold")
        human = raw.get("human_fields")
        return cls(
            fail_threshold=None if threshold is None else float(threshold),
            sample_rate=float(raw.get("sample_rate", 0.25)),
            sample_seed=str(raw.get("sample_seed", "human-review-v1")),
            csv=bool(raw.get("csv", True)),
            include_judge_details=bool(raw.get("include_judge_details", True)),
            include_all_rows=bool(raw.get("include_all_rows", True)),
            human_fields=[str(x) for x in human] if human else list(HUMAN_FIELDS),
            prefer_best_translation=bool(raw.get("prefer_best_translation", False)),
            queue_regressions=bool(raw.get("queue_regressions", False)),
            csv_all_rows=bool(raw.get("csv_all_rows", False)),
        )


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    benchmark: str
    source: SourceConfig
    languages: list[str]
    language_codes: dict[str, str]
    language_scripts: dict[str, str]
    field_groups: Any                       # list[list[str]] or dict[str, list[list[str]]]
    stages: dict[str, StageConfig]
    output: OutputConfig
    pricing: dict[str, dict]
    review: ReviewConfig = field(default_factory=ReviewConfig)
    vertex: VertexConfig = field(default_factory=VertexConfig)
    path: str = ""

    # ---- field groups -------------------------------------------------- #
    def groups_for(self, unit: Unit) -> list[list[str]]:
        """Field groups to translate for a unit, most specific match wins."""
        spec = self.field_groups
        if isinstance(spec, list):
            return [list(g) for g in spec]
        for key in (unit.label, unit.split, unit.config, "default"):
            if key in spec:
                return [list(g) for g in spec[key]]
        raise ValueError(
            f"config: no 'fields' entry matches {unit.label!r} "
            f"(tried {unit.label}, {unit.split}, {unit.config}, default)"
        )

    def code_for(self, language: str) -> str:
        return self.language_codes.get(language, language)

    def script_for(self, language: str) -> str:
        """Writing system for a language, for prompts that pin the output script."""
        return self.language_scripts.get(language) or DEFAULT_SCRIPTS.get(language, language)

    def stage(self, name: str) -> StageConfig:
        if name not in self.stages:
            raise ValueError(f"config: stage '{name}' is not configured in {self.path}")
        return self.stages[name]


def load_config(config_path: str) -> Config:
    path = Path(config_path).expanduser().resolve()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    base = path.parent

    if "vertex" in raw:
        raise ValueError(
            "config: Vertex settings no longer live in benchmark configs -- remove the "
            "'vertex:' block and set GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, "
            "GOOGLE_APPLICATION_CREDENTIALS, GCS_BUCKET and GCS_PREFIX in the "
            "environment or .env instead (see .env.example)"
        )

    benchmark = str(_require(raw, "benchmark", "top level"))
    source = SourceConfig.from_dict(_require(raw, "source", "top level"), base)

    languages_raw = _require(raw, "languages", "top level")
    if isinstance(languages_raw, dict):
        # {Hindi: hi, Bengali: bn}
        languages = [str(k) for k in languages_raw]
        language_codes = {str(k): str(v) for k, v in languages_raw.items()}
    else:
        languages = [str(x) for x in languages_raw]
        language_codes = {str(k): str(v) for k, v in (raw.get("language_codes") or {}).items()}

    language_scripts = {str(k): str(v) for k, v in (raw.get("language_scripts") or {}).items()}

    field_groups = _require(raw, "fields", "top level")

    stage_defaults = raw.get("stage_defaults") or {}
    stages_raw = raw.get("stages") or {}
    stages = {
        name: StageConfig.from_dict(name, stages_raw.get(name), base, stage_defaults)
        for name in STAGES
        if name in stages_raw or name == "translate"
    }

    cfg = Config(
        benchmark=benchmark,
        source=source,
        languages=languages,
        language_codes=language_codes,
        language_scripts=language_scripts,
        field_groups=field_groups,
        stages=stages,
        output=OutputConfig.from_dict(raw.get("output"), base),
        pricing=raw.get("pricing") or {},
        review=ReviewConfig.from_dict(raw.get("review")),
        vertex=VertexConfig.from_env(),
        path=str(path),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not cfg.languages:
        raise ValueError("config: 'languages' is empty")
    if not cfg.source.units:
        raise ValueError("config: source resolved to zero (config, split) units")
    for name, stage in cfg.stages.items():
        if not stage.enabled:
            continue
        if not stage.model:
            raise ValueError(f"config: stages.{name}.model is required")
        if not stage.prompt_path:
            raise ValueError(f"config: stages.{name}.prompt is required")
        if not Path(stage.prompt_path).exists():
            raise ValueError(f"config: stages.{name}.prompt not found: {stage.prompt_path}")
        if stage.output_format not in ("json", "sections"):
            raise ValueError(f"config: stages.{name}.output_format must be 'json' or 'sections'")
    if not 0.0 <= cfg.review.sample_rate <= 1.0:
        raise ValueError("config: review.sample_rate must be between 0 and 1")
    if cfg.output.destination not in ("local", "huggingface"):
        raise ValueError("config: output.destination must be 'local' or 'huggingface'")
    if cfg.output.destination == "huggingface" and not cfg.output.hub_repo:
        raise ValueError("config: output.hub_repo is required when output.destination is 'huggingface'")
