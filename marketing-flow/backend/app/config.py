from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    media_root: Path = Path(os.getenv("MEDIA_ROOT", "media")).resolve()
    spreadsheet_id: str = os.getenv("GOOGLE_SPREADSHEET_ID", "")
    api_key: str = os.getenv("MFA_API_KEY", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
    cors_origins: tuple[str, ...] = _csv(
        "CORS_ORIGINS", "http://localhost:3000,http://localhost:8501"
    )
    n8n_analysis_webhook: str = os.getenv("N8N_ANALYSIS_WEBHOOK", "")
    n8n_edit_webhook: str = os.getenv("N8N_EDIT_WEBHOOK", "")
    n8n_publish_webhook: str = os.getenv("N8N_PUBLISH_WEBHOOK", "")
    n8n_report_webhook: str = os.getenv("N8N_REPORT_WEBHOOK", "")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "500"))
    job_db_path: Path = Path(
        os.getenv("JOB_DB_PATH", str(Path(os.getenv("MEDIA_ROOT", "media")) / "jobs.sqlite3"))
    ).resolve()
    job_retention_days: int = int(os.getenv("JOB_RETENTION_DAYS", "14"))

    def webhook_for_sheet(self, sheet_name: str) -> str:
        if sheet_name == "Source Phân tích Video":
            return self.n8n_analysis_webhook
        if sheet_name == "Source Chỉnh sửa Video":
            return self.n8n_edit_webhook
        return ""


settings = Settings()
