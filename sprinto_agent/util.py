"""Small shared helpers: time, ids, hashing, domain handling, log redaction."""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def content_hash(subject: str, body: str) -> str:
    return hashlib.sha256((subject + "\n\x00\n" + body).encode("utf-8")).hexdigest()


def normalize_ws(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def registrable_domain(url_or_host: str) -> str:
    """Best-effort host normalisation: strip scheme, www., port. (No PSL dependency.)"""
    s = url_or_host.strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches(host: str, domain: str) -> bool:
    host, domain = registrable_domain(host), registrable_domain(domain)
    return host == domain or host.endswith("." + domain)


def same_site(url: str, domain: str) -> bool:
    return host_matches(urlparse(url).hostname or "", domain)


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_KEYLIKE = re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization|bearer)([\"'=:\s]+)([^\s&\"']+)")
_KNOWN_KEYS = re.compile(r"\b(tvly-[A-Za-z0-9_\-]{8,}|napi_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}"
                         r"|GOCSPX-[A-Za-z0-9_\-]{8,}|ya29\.[A-Za-z0-9._\-]{8,})|(?<![\w/])1//[A-Za-z0-9_\-]{10,}")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]+=*")
_PHONE = re.compile(r"(?<![\w/])\+?\d[\d\s().\-]{7,}\d(?![\w/])")
_URL_CRED = re.compile(r"(postgres(?:ql)?://)[^@\s]+@")


def redact(text: str) -> str:
    """For logs and audit rows: removes emails, credentials and known API-key formats."""
    text = _URL_CRED.sub(r"\1[redacted]@", text)
    text = _BEARER.sub(r"\1 [redacted]", text)
    text = _KNOWN_KEYS.sub("[redacted-key]", text)
    text = _EMAIL.sub("[email]", text)
    return _KEYLIKE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)


def redact_pii(text: str) -> str:
    """For stored evidence quotes: removes personal emails and phone numbers that pages may contain."""
    def phone(m: "re.Match") -> str:
        t = m.group(0)
        digits = sum(ch.isdigit() for ch in t)
        if digits < 10 or re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", t.strip()):
            return t
        return "[phone]"
    return _PHONE.sub(phone, _EMAIL.sub("[email]", text))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover
            pass
        return True


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in log.filters):
        log.addFilter(RedactingFilter())
    return log


def parse_date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    fmts = ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"]
    t = text.replace("Z", "+00:00") if re.match(r"\d{4}-\d{2}-\d{2}T", text) else text
    for fmt in fmts:
        try:
            d = datetime.strptime(t, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
