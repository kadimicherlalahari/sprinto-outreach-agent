"""Dashboard login gate for deployed (non-localhost) use. Offline; mock store."""
from fastapi.testclient import TestClient

from sprinto_agent.config import Settings
from sprinto_agent.web import create_app


def client(store, monkeypatch, password=None, vercel=True, cron=None):
    for k in ("DASHBOARD_PASSWORD", "VERCEL", "CRON_SECRET", "DASHBOARD_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEMO_LOGIN", "off")           # demo credentials are tested separately below
    for k in ():
        monkeypatch.delenv(k, raising=False)
    if password:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    if vercel:
        monkeypatch.setenv("VERCEL", "1")
    if cron:
        monkeypatch.setenv("CRON_SECRET", cron)
    return TestClient(create_app(mock=True, store=store, settings=Settings(mock=True, operator_inbox="op@inbox.example")),
                      base_url="https://testserver")   # deployed cookies are Secure (HTTPS only)


def test_deployed_without_password_is_locked(store, monkeypatch):
    c = client(store, monkeypatch)
    for path in ("/", "/controls", "/review"):
        assert c.get(path).status_code == 503
    assert c.post("/settings/pause", data={"paused": "1"}).status_code == 503


def test_password_required_and_checked(store, monkeypatch):
    c = client(store, monkeypatch, password="correct horse")
    r = c.get("/controls", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.post("/settings/pause", data={"paused": "1"}).status_code == 401          # no session: writes refused
    bad = c.post("/login", data={"password": "nope", "next": "/"}, follow_redirects=False)
    assert "error" in bad.headers["location"]
    ok = c.post("/login", data={"password": "correct horse", "next": "/controls"}, follow_redirects=False)
    assert ok.headers["location"] == "/controls" and "sprinto_session" in ok.headers.get("set-cookie", "")
    assert "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
    assert c.get("/controls").status_code == 200


def test_forged_or_expired_cookie_rejected(store, monkeypatch):
    from sprinto_agent import auth
    c = client(store, monkeypatch, password="pw")
    c.cookies.set(auth.COOKIE, "9999999999.deadbeef")
    assert c.get("/", follow_redirects=False).status_code == 303
    c.cookies.set(auth.COOKIE, auth.make_token(now=0))                                  # expired long ago
    assert c.get("/", follow_redirects=False).status_code == 303


def test_open_redirect_blocked_after_login(store, monkeypatch):
    c = client(store, monkeypatch, password="pw")
    r = c.post("/login", data={"password": "pw", "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_runs_on_vercel_finish_inside_the_request_and_cron_needs_secret(store, monkeypatch):
    c = client(store, monkeypatch, password="pw", cron="cron-s3cret")
    c.post("/login", data={"password": "pw", "next": "/"})
    r = c.post("/run", data={"domains": "ledgerly.example"}, follow_redirects=False)
    assert r.headers["location"].startswith("/runs/")                                # no background thread on Vercel
    assert store.one("SELECT status FROM research_runs")["status"] == "completed"
    assert c.get("/cron/gmail-sync").status_code == 403
    assert c.get("/cron/gmail-sync", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert c.get("/cron/gmail-sync", headers={"Authorization": "Bearer cron-s3cret"}).status_code == 200


def test_local_without_password_unchanged(store, monkeypatch):
    c = client(store, monkeypatch, vercel=False)
    assert c.get("/").status_code == 200


def test_demo_credentials_shown_and_accepted_on_vercel(store, monkeypatch):
    from sprinto_agent import auth
    c = client(store, monkeypatch, password="owner-secret")
    monkeypatch.setenv("DEMO_LOGIN", "on")
    page = c.get("/login").text
    assert auth.DEMO_USERNAME in page and auth.DEMO_PASSWORD in page
    bad = c.post("/login", data={"username": "someone", "password": auth.DEMO_PASSWORD, "next": "/"}, follow_redirects=False)
    assert "error" in bad.headers["location"]                                        # demo password needs the demo username
    ok = c.post("/login", data={"username": auth.DEMO_USERNAME, "password": auth.DEMO_PASSWORD, "next": "/"}, follow_redirects=False)
    assert ok.headers["location"] == "/" and c.get("/controls").status_code == 200
    c2 = client(store, monkeypatch, password="owner-secret")
    monkeypatch.setenv("DEMO_LOGIN", "on")
    assert c2.post("/login", data={"password": "owner-secret", "next": "/"}, follow_redirects=False).headers["location"] == "/"


def test_demo_login_can_be_turned_off(store, monkeypatch):
    from sprinto_agent import auth
    c = client(store, monkeypatch, password="owner-secret")
    assert auth.DEMO_PASSWORD not in c.get("/login").text
    r = c.post("/login", data={"username": auth.DEMO_USERNAME, "password": auth.DEMO_PASSWORD}, follow_redirects=False)
    assert "error" in r.headers["location"]
