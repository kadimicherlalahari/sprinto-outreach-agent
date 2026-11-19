"""Gmail API (OAuth) client used for sending to the operator's own inbox and for mailbox tracking.

Only two scopes are requested: gmail.send (send) and gmail.readonly (history, thread and message reads for
reply/bounce tracking). Nothing here can add a recipient: the message is built and recipient-checked in
review.build_message before it reaches this module, and send_raw re-checks the raw headers.

A successful send means Gmail ACCEPTED the message (delivery_status 'accepted_by_gmail'). It is never reported
as delivered to an inbox; bounces arrive later through mailbox sync.
"""
from __future__ import annotations

import base64
import json
import os
import time
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_TOKEN_PATH = DATA_DIR / "secrets" / "gmail_token.json"


class GmailError(Exception):
    """kind: auth | quota | outage | not_found | rejected | uncertain | not_configured"""

    def __init__(self, message: str, kind: str = "outage", status: Optional[int] = None):
        super().__init__(message)
        self.kind, self.status = kind, status


class HistoryExpired(GmailError):
    def __init__(self, message: str = "Gmail history id expired; full recovery sync needed"):
        super().__init__(message, "not_found", 404)


def _kind(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "quota"
    if status == 404:
        return "not_found"
    if 400 <= status < 500:
        return "rejected"
    return "outage"


# ---------------------------------------------------------------- token storage (local file, 0600, git-ignored)
def load_token(path: Optional[Path] = None) -> Dict[str, Any]:
    env_rt = os.environ.get("GMAIL_REFRESH_TOKEN")
    if env_rt:
        return {"refresh_token": env_rt.strip()}   # deployments without a token file (e.g. Vercel)
    p = Path(path or DEFAULT_TOKEN_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save_token(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path or DEFAULT_TOKEN_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


class RealGmail:
    def __init__(self, client_id: str, client_secret: str, token_path: Optional[Path] = None, timeout: float = 30.0):
        import httpx
        self._cid, self._secret = client_id, client_secret
        self._token_path = token_path
        self._http = httpx.Client(timeout=timeout)
        self._access: Optional[str] = None
        self._exp = 0.0

    # ----- auth -----
    def _token(self) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        tok = load_token(self._token_path)
        if not tok.get("refresh_token"):
            raise GmailError("Gmail is not authorised yet: run  python -m sprinto_agent gmail-auth", "not_configured")
        try:
            r = self._http.post(TOKEN_URL, data={"client_id": self._cid, "client_secret": self._secret,
                                                 "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        except Exception as e:
            raise GmailError("Google token endpoint unreachable (%s)" % type(e).__name__, "outage")
        if r.status_code != 200:
            raise GmailError("Google refused the stored Gmail authorisation (HTTP %d); run gmail-auth again"
                             % r.status_code, "auth", r.status_code)
        data = r.json()
        self._access, self._exp = data["access_token"], time.time() + float(data.get("expires_in", 3000))
        return self._access

    def _req(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        try:
            r = self._http.request(method, API + path, headers={"Authorization": "Bearer " + self._token()}, **kw)
        except GmailError:
            raise
        except Exception as e:
            raise GmailError("Gmail unreachable (%s)" % type(e).__name__, "outage")
        if r.status_code >= 300:
            raise GmailError("Gmail HTTP %d on %s" % (r.status_code, path.split("?")[0]), _kind(r.status_code), r.status_code)
        return r.json() if r.content else {}

    # ----- send -----
    def send_raw(self, raw: bytes) -> Dict[str, Any]:
        """POST messages.send. Errors are classified for the at-most-once logic in review.send:
        a response with an error status means Gmail did not accept it (definite); a lost connection after the
        request may have been sent is 'uncertain'."""
        import httpx
        body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        try:
            token = self._token()
        except GmailError as e:
            raise GmailError(str(e), e.kind if e.kind != "outage" else "rejected")   # nothing was sent yet
        try:
            r = self._http.post(API + "/messages/send", json=body, headers={"Authorization": "Bearer " + token})
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise GmailError("could not connect to Gmail (%s); nothing was sent" % type(e).__name__, "rejected")
        except Exception as e:
            raise GmailError("connection to Gmail lost after sending the request (%s)" % type(e).__name__, "uncertain")
        if r.status_code >= 500:
            raise GmailError("Gmail HTTP %d while sending" % r.status_code, "uncertain", r.status_code)
        if r.status_code >= 300:
            raise GmailError("Gmail refused the message (HTTP %d)" % r.status_code, "rejected", r.status_code)
        return r.json()

    # ----- mailbox -----
    def profile(self) -> Dict[str, Any]:
        return self._req("GET", "/profile")

    def watch(self, topic: str) -> Dict[str, Any]:
        return self._req("POST", "/watch", json={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"})

    def history(self, start_history_id: str) -> Dict[str, Any]:
        """-> {"history_id": latest, "message_ids": [...]} across pages. Raises HistoryExpired on 404."""
        ids: List[str] = []
        token, latest = None, start_history_id
        while True:
            params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": 500}
            if token:
                params["pageToken"] = token
            try:
                data = self._req("GET", "/history", params=params)
            except GmailError as e:
                if e.status == 404:
                    raise HistoryExpired()
                raise
            for h in data.get("history", []) or []:
                for m in h.get("messagesAdded", []) or []:
                    mid = (m.get("message") or {}).get("id")
                    if mid and mid not in ids:
                        ids.append(mid)
            latest = data.get("historyId", latest)
            token = data.get("nextPageToken")
            if not token:
                return {"history_id": str(latest), "message_ids": ids}

    def get_message(self, message_id: str) -> Dict[str, Any]:
        return parse_api_message(self._req("GET", "/messages/%s" % message_id, params={"format": "raw"}))

    def get_thread(self, thread_id: str) -> List[Dict[str, Any]]:
        data = self._req("GET", "/threads/%s" % thread_id, params={"format": "minimal"})
        return [self.get_message(m["id"]) for m in data.get("messages", []) or []]

    def find_rfc822(self, rfc822_id: str) -> List[str]:
        data = self._req("GET", "/messages", params={"q": "rfc822msgid:%s" % rfc822_id.strip("<>"), "includeSpamTrash": "true"})
        return [m["id"] for m in data.get("messages", []) or []]


def parse_api_message(m: Dict[str, Any]) -> Dict[str, Any]:
    """Gmail API message (format=raw) -> normalized dict. Content is untrusted data."""
    raw = base64.urlsafe_b64decode((m.get("raw") or "").encode("ascii") + b"==") if m.get("raw") else b""
    return parse_raw(raw, m.get("id"), m.get("threadId"), m.get("labelIds") or [], m.get("historyId"),
                     m.get("internalDate"))


def parse_raw(raw: bytes, mid: Optional[str], thread_id: Optional[str], labels: List[str],
              history_id: Any = None, internal_date: Any = None) -> Dict[str, Any]:
    msg = message_from_bytes(raw, policy=policy.default) if raw else None
    headers: Dict[str, str] = {}
    text, has_calendar, is_report = "", False, False
    if msg is not None:
        for k in ("From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References", "Auto-Submitted",
                  "Content-Type", "X-Sprinto-Send-Id", "X-Failed-Recipients"):
            v = msg.get(k)
            if v is not None:
                headers[k] = str(v)[:500]
        ctype = msg.get_content_type()
        is_report = ctype == "multipart/report"
        for part in msg.walk():
            pt = part.get_content_type()
            if pt == "text/calendar" or (part.get_filename() or "").lower().endswith(".ics"):
                try:
                    cal = part.get_content()
                    cal = cal.decode("utf-8", "ignore") if isinstance(cal, bytes) else str(cal)
                except Exception:
                    cal = ""
                if "METHOD:REQUEST" in cal.upper():
                    has_calendar = True
            elif pt == "message/delivery-status":
                is_report = True
            elif pt == "text/plain" and not text:
                try:
                    text = str(part.get_content())
                except Exception:
                    text = ""
    return {"id": mid, "thread_id": thread_id, "labels": list(labels), "history_id": str(history_id or ""),
            "internal_date": str(internal_date or ""), "headers": headers, "text": text[:20000],
            "has_calendar_invite": has_calendar, "is_delivery_report": is_report}


# ---------------------------------------------------------------- fake (tests + mock mode; never networks)
class FakeGmail:
    """In-memory mailbox with history ids. Failure modes for tests:
    send_mode: ok | reject | uncertain | accept_then_drop (Gmail accepted, then the connection dropped)."""

    def __init__(self, address: str = "operator@inbox.example"):
        self.address = address
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.history_log: List[tuple] = []   # (history_id, message_id)
        self.hid = 1000
        self.n = 0
        self.send_mode = "ok"
        self.watch_fail = False
        self.history_expired = False
        self.outage = False
        self.watch_calls = 0
        self.sent_raw: List[bytes] = []

    def _check(self) -> None:
        if self.outage:
            raise GmailError("Gmail unreachable (fake outage)", "outage")

    def _add(self, raw: bytes, labels: List[str], thread_id: Optional[str] = None) -> Dict[str, Any]:
        self.n += 1
        self.hid += 1
        mid = "m%04d" % self.n
        msg = message_from_bytes(raw, policy=policy.default)
        if thread_id is None:
            ref = (msg.get("In-Reply-To") or "").strip()
            for other in self.messages.values():
                if ref and other["headers"].get("Message-ID", "").strip() == ref:
                    thread_id = other["thread_id"]
                    break
        thread_id = thread_id or "t%04d" % self.n
        rec = parse_raw(raw, mid, thread_id, labels, self.hid, int(time.time() * 1000))
        self.messages[mid] = rec
        self.history_log.append((self.hid, mid))
        return rec

    def send_raw(self, raw: bytes) -> Dict[str, Any]:
        if self.send_mode == "reject":
            raise GmailError("Gmail refused the message (HTTP 400)", "rejected", 400)
        if self.send_mode == "uncertain":
            raise GmailError("connection to Gmail lost after sending the request (ReadTimeout)", "uncertain")
        self.sent_raw.append(raw)
        rec = self._add(raw, ["SENT", "INBOX"])     # own-inbox mode: the copy also lands in the inbox
        if self.send_mode == "accept_then_drop":
            raise GmailError("connection to Gmail lost after sending the request (ReadTimeout)", "uncertain")
        return {"id": rec["id"], "threadId": rec["thread_id"], "labelIds": ["SENT"]}

    def deliver(self, raw: bytes, thread_id: Optional[str] = None, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        """Simulate an incoming message (reply, bounce, invite)."""
        return self._add(raw, labels or ["INBOX"], thread_id)

    def profile(self) -> Dict[str, Any]:
        self._check()
        return {"emailAddress": self.address, "historyId": str(self.hid)}

    def watch(self, topic: str) -> Dict[str, Any]:
        self.watch_calls += 1
        if self.watch_fail:
            raise GmailError("Gmail HTTP 403 on /watch", "auth", 403)
        return {"historyId": str(self.hid), "expiration": str(int((time.time() + 7 * 86400) * 1000))}

    def history(self, start_history_id: str) -> Dict[str, Any]:
        self._check()
        if self.history_expired:
            raise HistoryExpired()
        start = int(start_history_id)
        ids = [mid for h, mid in self.history_log if h > start]
        return {"history_id": str(self.hid), "message_ids": ids}

    def get_message(self, message_id: str) -> Dict[str, Any]:
        self._check()
        if message_id not in self.messages:
            raise GmailError("Gmail HTTP 404 on /messages", "not_found", 404)
        return dict(self.messages[message_id])

    def get_thread(self, thread_id: str) -> List[Dict[str, Any]]:
        self._check()
        return [dict(m) for m in self.messages.values() if m["thread_id"] == thread_id]

    def find_rfc822(self, rfc822_id: str) -> List[str]:
        self._check()
        return [mid for mid, m in self.messages.items() if m["headers"].get("Message-ID", "").strip("<> ") == rfc822_id.strip("<> ")]


def build_client(settings: Any) -> Optional[Any]:
    if getattr(settings, "mock", False):
        return None
    if not (settings.gmail_client_id and settings.gmail_client_secret):
        return None
    return RealGmail(settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_token_path)


# ---------------------------------------------------------------- one-time OAuth (installed app, loopback)
def authorize(client_id: str, client_secret: str, token_path: Optional[Path] = None, port: int = 8765) -> str:
    """Opens the Google consent page; the operator signs in themselves. Stores only the refresh token locally."""
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlencode, urlparse

    import httpx
    state = secrets.token_urlsafe(16)
    redirect = "http://127.0.0.1:%d/" % port
    url = AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                                      "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
                                      "state": state})
    got: Dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Gmail authorisation received. You can close this tab.")

        def log_message(self, *a):
            pass

    print("Open this URL and sign in to the Gmail account that is your OPERATOR_INBOX:\n\n%s\n" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv = HTTPServer(("127.0.0.1", port), H)
    srv.timeout = 300
    srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise GmailError("authorisation was cancelled or the response did not match this request", "auth")
    r = httpx.post(TOKEN_URL, data={"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                    "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        raise GmailError("token exchange failed (HTTP %d)" % r.status_code, "auth", r.status_code)
    data = r.json()
    if not data.get("refresh_token"):
        raise GmailError("Google did not return a refresh token; remove the app's access in your Google account and retry", "auth")
    save_token({"refresh_token": data["refresh_token"], "scope": data.get("scope", "")}, token_path)
    return str(token_path or DEFAULT_TOKEN_PATH)
