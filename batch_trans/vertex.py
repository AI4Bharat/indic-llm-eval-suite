"""Vertex AI access: service-account credentials, the genai client, and GCS.

Everything here authenticates the same way -- a service account JSON key, or
Application Default Credentials when no key file is given.  No API keys are
involved anywhere in the pipeline.

**All of it comes from the environment**, never from a benchmark config: which
project you bill, which region you run in, which key file you hold and which
bucket you stage through are properties of the machine and the account, not of
GSM8K.  Keeping them out of the YAML means configs are safe to commit and to
hand to someone else, and that one person can run a colleague's config against
their own project without editing it.

Read from (first match wins):

    GOOGLE_CLOUD_PROJECT / PROJECT_ID        project id
    GOOGLE_CLOUD_LOCATION / LOCATION         region, default us-central1
    GOOGLE_APPLICATION_CREDENTIALS           service account JSON; unset = ADC
    GCS_BUCKET                               staging bucket for batch mode
    GCS_PREFIX                               key prefix in that bucket

Batch prediction on Vertex reads its input from GCS and writes its output back
to GCS, so a bucket is required for batch mode (and must be in a region
compatible with ``location``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


@dataclass
class VertexConfig:
    """Vertex AI / GCS settings, read from the environment."""
    project: str | None = None
    location: str = "us-central1"
    credentials_file: str | None = None     # service account JSON; else ADC
    gcs_bucket: str | None = None           # bucket for batch input/output, no gs:// prefix
    gcs_prefix: str = "batch_trans"         # key prefix inside the bucket

    @classmethod
    def from_env(cls) -> "VertexConfig":
        credentials = _env("GOOGLE_APPLICATION_CREDENTIALS")
        return cls(
            project=_env("GOOGLE_CLOUD_PROJECT", "PROJECT_ID") or None,
            location=_env("GOOGLE_CLOUD_LOCATION", "LOCATION", default="us-central1"),
            credentials_file=os.path.expanduser(credentials) if credentials else None,
            gcs_bucket=_env("GCS_BUCKET").replace("gs://", "").strip("/") or None,
            gcs_prefix=_env("GCS_PREFIX", default="batch_trans").strip("/"),
        )

    def describe(self) -> str:
        return (
            f"project={self.project} location={self.location} "
            f"bucket={self.gcs_bucket} credentials="
            f"{self.credentials_file or 'application-default'}"
        )

    def require(self, what: str) -> None:
        missing = [
            name for name, value in
            (("GOOGLE_CLOUD_PROJECT", self.project), ("GCS_BUCKET", self.gcs_bucket))
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{what} needs {' and '.join(missing)} in the environment "
                f"(put them in .env -- see .env.example)"
            )
        if self.credentials_file and not Path(self.credentials_file).exists():
            raise RuntimeError(
                f"GOOGLE_APPLICATION_CREDENTIALS points at a file that does not exist: "
                f"{self.credentials_file}"
            )

    def uri(self, *parts: str) -> str:
        path = "/".join(p.strip("/") for p in (self.gcs_prefix, *parts) if p)
        return f"gs://{self.gcs_bucket}/{path}"


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #

def load_credentials(cfg: VertexConfig):
    """Service account credentials from ``credentials_file``, or None for ADC."""
    if not cfg.credentials_file:
        return None
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(cfg.credentials_file, scopes=SCOPES)


def genai_client(cfg: VertexConfig):
    from google import genai

    return genai.Client(
        vertexai=True,
        project=cfg.project,
        location=cfg.location,
        credentials=load_credentials(cfg),
    )


def storage_client(cfg: VertexConfig):
    try:
        from google.cloud import storage
    except ImportError as e:
        raise RuntimeError(
            "batch mode needs the GCS client: pip install google-cloud-storage"
        ) from e

    return storage.Client(project=cfg.project, credentials=load_credentials(cfg))


# --------------------------------------------------------------------------- #
# GCS helpers
# --------------------------------------------------------------------------- #

def split_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/a/b`` -> ``("bucket", "a/b")``."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a GCS URI: {uri}")
    bucket, _, blob = uri[len("gs://"):].partition("/")
    return bucket, blob


def upload(cfg: VertexConfig, local_path: Path, uri: str) -> str:
    bucket_name, blob_name = split_uri(uri)
    client = storage_client(cfg)
    client.bucket(bucket_name).blob(blob_name).upload_from_filename(str(local_path))
    return uri


def list_uris(cfg: VertexConfig, prefix_uri: str, suffix: str = ".jsonl") -> list[str]:
    bucket_name, prefix = split_uri(prefix_uri)
    client = storage_client(cfg)
    return [
        f"gs://{bucket_name}/{blob.name}"
        for blob in client.list_blobs(bucket_name, prefix=prefix.rstrip("/") + "/")
        if blob.name.endswith(suffix)
    ]


def download(cfg: VertexConfig, uri: str, local_path: Path) -> Path:
    bucket_name, blob_name = split_uri(uri)
    client = storage_client(cfg)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    client.bucket(bucket_name).blob(blob_name).download_to_filename(str(local_path))
    return local_path
