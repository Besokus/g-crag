from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_USER_", env_file=".env", extra="ignore", case_sensitive=False
    )

    APP_NAME: str = "Grid-QA LLM as a User"
    APP_VERSION: str = "0.1.0"
    ROLE: str = "all"  # all | api | worker | scheduler
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/llm_user_suite.db"
    ARTIFACT_DIR: str = "./data/artifacts"

    EVENT_RETENTION_DAYS: int = 30
    REPORT_RETENTION_DAYS: int = 180
    SESSION_IDLE_SECONDS: int = 1800
    DREAM_INTERVAL_SECONDS: int = 21600
    DREAM_MIN_CLUSTER_SIZE: int = 3
    FAITHFULNESS_GATE: float = 0.85
    SCENARIO_DRIFT_SIMILARITY: float = 0.85

    METRICS_SCRAPE_URL: str = ""
    METRICS_SCRAPE_INTERVAL_SECONDS: int = 60
    OTLP_MAX_REQUEST_BYTES: int = 16 * 1024 * 1024
    METRIC_ALLOWLIST: str = (
        "grid_http_requests_total,grid_http_latency_seconds,grid_qa_total,"
        "grid_llm_calls_total,grid_llm_latency_seconds,grid_llm_fallback_total,"
        "grid_retrieval_latency_seconds,grid_feedback_total,grid_errors_total,"
        "grid_degraded_total,grid_faithfulness_trend"
    )
    METRIC_LABEL_ALLOWLIST: str = "method,status,provider,feedback,reason,route,model,cached"

    TARGET_BASE_URL: str = ""
    TARGET_ENVIRONMENT: str = "test"
    ALLOWED_TARGET_HOSTS: str = ""
    DENIED_TARGET_HOSTS: str = ""
    REQUIRE_TARGET_ALLOWLIST: bool = True
    CREDENTIAL_PROFILES_JSON: str = "{}"
    MAX_RUN_SECONDS: int = 900
    MAX_API_CALLS: int = 30
    RUN_CONCURRENCY: int = 2
    RUNNER_BACKEND: str = "local"  # local | kubernetes
    RUNNER_NAMESPACE: str = ""
    RUNNER_IMAGE: str = ""
    RUNNER_SERVICE_ACCOUNT: str = "llm-user-runner"
    RUNNER_ENV_CONFIGMAP: str = "llm-user-suite-config"
    RUNNER_ENV_SECRET: str = "llm-user-suite-env"
    RUNNER_RECONCILE_INTERVAL_SECONDS: int = 30
    TASK_LOCK_TIMEOUT_SECONDS: int = 1800

    GRID_AUTH_BASE_URL: str = ""
    GRID_CALLBACK_URL: str = ""
    GRID_CALLBACK_SECRET: str = ""
    GRID_EVENT_SECRET: str = ""
    AUTH_DISABLED: bool = False

    DREAMER_BASE_URL: str = ""
    DREAMER_API_KEY: str = ""
    DREAMER_MODEL: str = ""
    ACTOR_BASE_URL: str = ""
    ACTOR_API_KEY: str = ""
    ACTOR_MODEL: str = ""
    JUDGE_BASE_URL: str = ""
    JUDGE_API_KEY: str = ""
    JUDGE_MODEL: str = ""
    CHALLENGER_BASE_URL: str = ""
    CHALLENGER_API_KEY: str = ""
    CHALLENGER_MODEL: str = ""
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434/v1"
    OLLAMA_MODEL: str = "qwen2.5:1.5b-instruct-q4_K_M"
    MODEL_TIMEOUT_SECONDS: float = 120.0

    RAW_CAPTURE_ENABLE: bool = False
    RAW_CAPTURE_KEY: str = ""
    RAW_CAPTURE_KEY_ID: str = "local-secret-v1"
    RAW_CAPTURE_REQUIRE_KMS: bool = True
    RAW_CAPTURE_KMS_URL: str = ""
    RAW_CAPTURE_KMS_TOKEN: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET: str = "llm-user-artifacts"
    S3_SECURE: bool = False
    USER_HASH_SECRET: str = ""
    REDACT_FIELDS: list[str] = Field(default_factory=lambda: [
        "password", "passwd", "authorization", "cookie", "token", "secret",
        "api_key", "apikey", "access_key", "refresh_token", "jwt",
        "kms_token",
    ])

    def allowed_target_hosts(self) -> set[str]:
        return {item.strip().lower() for item in self.ALLOWED_TARGET_HOSTS.split(",") if item.strip()}

    def denied_target_hosts(self) -> set[str]:
        return {item.strip().lower() for item in self.DENIED_TARGET_HOSTS.split(",") if item.strip()}

    def metric_allowlist(self) -> set[str]:
        return {item.strip() for item in self.METRIC_ALLOWLIST.split(",") if item.strip()}

    def metric_label_allowlist(self) -> set[str]:
        return {item.strip() for item in self.METRIC_LABEL_ALLOWLIST.split(",") if item.strip()}

    def credential_profiles(self) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(self.CREDENTIAL_PROFILES_JSON or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def artifact_path(self) -> Path:
        path = Path(self.ARTIFACT_DIR).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
