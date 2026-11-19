"""Login gate for the dashboard when it is reachable beyond localhost (e.g. on Vercel).

* DASHBOARD_PASSWORD set  -> every page needs a signed session cookie obtained at /login.
* Running on Vercel (VERCEL env var) and no password -> every request is refused (fail closed).
* Local runs without a password keep working as before (the server binds to 127.0.0.1 only).
Exempt paths authenticate themselves: /gmail/push (shared token), /cron/* (CRON_SECRET bearer), /login.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

COOKIE = "sprinto_session"
MAX_AGE = 12 * 3600
EXEMPT = ("/login", "/gmail/push", "/cron/", "/favicon.ico")


def on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def password() -> Optional[str]:
    return (os.environ.get("DASHBOARD_PASSWORD") or "").strip() or None


def _secret() -> bytes:
    s = os.environ.get("DASHBOARD_SESSION_SECRET") or ("sprinto:" + (password() or ""))
    return hashlib.sha256(s.encode()).digest()


def make_token(now: Optional[float] = None) -> str:
    exp = str(int((time.time() if now is None else now) + MAX_AGE))
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return exp + "." + sig


def valid_token(tok: Optional[str], now: Optional[float] = None) -> bool:
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > (time.time() if now is None else now)


def check_password(given: str) -> bool:
    pw = password()
    return bool(pw) and hmac.compare_digest((given or "").encode(), pw.encode())


def gate(path: str, cookie: Optional[str]) -> Optional[str]:
    """None = allowed; 'login' = redirect to /login; 'closed' = refuse (no password configured on Vercel)."""
    if any(path == p or path.startswith(p) for p in EXEMPT):
        return None
    if password():
        return None if valid_token(cookie) else "login"
    return "closed" if on_vercel() else None
