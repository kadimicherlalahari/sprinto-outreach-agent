"""Settings and config loading. Secrets come only from environment / .env."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env", override=False)


@dataclass
class Settings:
    mock: bool = False
    neon_url: Optional[str] = None
    tavily_key: Optional[str] = None
    gemini_key: Optional[str] = None
    gemini_model: str = "gemini-2.5-flash"
    hunter_key: Optional[str] = None
    send_mode: str = "dry-run"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = field(default=None, repr=False)
    operator_inbox: Optional[str] = None
    sender_name: str = ""
    follow_up_days: int = 3
    user_agent: str = "SprintoProspectingResearch/0.1"
    max_pages_per_company: int = 12
    max_research_rounds: int = 4
    max_searches_per_company: int = 3
    max_llm_calls_per_company: int = 12
    max_seconds_per_company: float = 180.0
    stale_run_minutes: float = 15.0
    request_timeout: float = 15.0
    per_host_delay: float = 1.5
    send_transport: str = "gmail_api"            # gmail_api (OAuth) | smtp (legacy app password)
    gmail_client_id: Optional[str] = None
    gmail_client_secret: Optional[str] = field(default=None, repr=False)
    gmail_token_path: Optional[Path] = None
    gmail_pubsub_topic: Optional[str] = None     # projects/<id>/topics/<name> for push notifications
    gmail_push_token: Optional[str] = field(default=None, repr=False)   # shared secret on the push endpoint
    gmail_sync_minutes: float = 5.0

    def __repr__(self) -> str:  # never print secrets
        return "Settings(mock=%s, send_mode=%s, transport=%s, neon=%s, tavily=%s, gemini=%s, hunter=%s, smtp=%s, gmail=%s, inbox=%s)" % (
            self.mock, self.send_mode, self.send_transport, bool(self.neon_url), bool(self.tavily_key),
            bool(self.gemini_key), bool(self.hunter_key), bool(self.smtp_user), bool(self.gmail_client_id),
            bool(self.operator_inbox))

    def missing(self) -> Dict[str, str]:
        """Human-readable list of missing configuration (for real runs)."""
        out = {}
        if not self.neon_url:
            out["NEON_DATABASE_URL"] = "no database; real runs cannot be saved"
        if not self.tavily_key:
            out["TAVILY_API_KEY"] = "search-based discovery disabled (feeds/portfolio/domain list still work)"
        if not self.gemini_key:
            out["GEMINI_API_KEY"] = "LLM extraction/wording disabled; rule-based extraction and templates used"
        if not self.hunter_key:
            out["HUNTER_API_KEY"] = "Hunter lookup disabled; manual Apollo contact entry required"
        if not self.operator_inbox:
            out["OPERATOR_INBOX"] = "sending disabled"
        if not (self.gmail_client_id and self.gmail_client_secret):
            out["GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET"] = "Gmail API sending and reply tracking disabled"
        return out


def load_settings(mock: bool = False) -> Settings:
    _load_dotenv()
    env = os.environ.get

    def _f(name: str, default: float) -> float:
        try:
            return float(env(name, default))
        except ValueError:
            return default

    return Settings(
        mock=mock,
        neon_url=env("NEON_DATABASE_URL") or None,
        tavily_key=env("TAVILY_API_KEY") or None,
        gemini_key=env("GEMINI_API_KEY") or None,
        gemini_model=env("GEMINI_MODEL") or "gemini-2.5-flash",
        hunter_key=env("HUNTER_API_KEY") or None,
        send_mode=(env("SEND_MODE") or "dry-run").strip().lower(),
        smtp_host=env("SMTP_HOST") or "smtp.gmail.com",
        smtp_port=int(_f("SMTP_PORT", 587)),
        smtp_user=env("SMTP_USER") or None,
        smtp_password=env("SMTP_APP_PASSWORD") or None,
        operator_inbox=(env("OPERATOR_INBOX") or "").strip() or None,
        sender_name=(env("SENDER_NAME") or "").strip(),
        follow_up_days=int(_f("FOLLOW_UP_DAYS", 3)),
        user_agent=env("USER_AGENT") or "SprintoProspectingResearch/0.1",
        max_pages_per_company=int(_f("MAX_PAGES_PER_COMPANY", 12)),
        max_research_rounds=int(_f("MAX_RESEARCH_ROUNDS", 4)),
        max_searches_per_company=int(_f("MAX_SEARCHES_PER_COMPANY", 3)),
        max_llm_calls_per_company=int(_f("MAX_LLM_CALLS_PER_COMPANY", 12)),
        max_seconds_per_company=_f("MAX_SECONDS_PER_COMPANY", 180),
        stale_run_minutes=_f("STALE_RUN_MINUTES", 15),
        request_timeout=_f("REQUEST_TIMEOUT_SECONDS", 15),
        per_host_delay=_f("PER_HOST_DELAY_SECONDS", 1.5),
        send_transport=(env("SEND_TRANSPORT") or "gmail_api").strip().lower(),
        gmail_client_id=env("GMAIL_CLIENT_ID") or None,
        gmail_client_secret=env("GMAIL_CLIENT_SECRET") or None,
        gmail_token_path=Path(env("GMAIL_TOKEN_PATH")) if env("GMAIL_TOKEN_PATH") else None,
        gmail_pubsub_topic=env("GMAIL_PUBSUB_TOPIC") or None,
        gmail_push_token=env("GMAIL_PUSH_TOKEN") or None,
        gmail_sync_minutes=_f("GMAIL_SYNC_MINUTES", 5),
    )


def load_yaml(name: str, config_dir: Optional[Path] = None) -> Dict[str, Any]:
    path = (config_dir or CONFIG_DIR) / name
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_icp(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    return load_yaml("icp.yaml", config_dir)


def load_sources(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    return load_yaml("sources.yaml", config_dir)
