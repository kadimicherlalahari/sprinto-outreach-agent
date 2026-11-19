"""Cost caps and the spend ledger for billable calls (Tavily search, Gemini model calls, Hunter lookups).

Rules (enforced here, tested in tests/test_costs.py):
  * No dollar amounts are invented. The operator sets caps and unit prices in Settings; until a per-company
    cap, a period cap and, for every billable provider in use, either a unit price or a unit limit are set,
    paid research does not run (CostNotConfigured).
  * Before every billable call a reservation is written to cost_ledger inside a transaction that holds a
    row lock, so concurrent workers cannot overshoot a cap. The call is settled afterwards with real units
    when the provider reports them (Gemini token usage), otherwise with the estimate.
  * A provider whose price is unknown is recorded with cost_basis 'unknown_price' and NULL cost (never 0)
    and is limited by a separate per-period unit limit.
  * Costs are 'estimated' (operator price x units). 'actual' is only used if a provider reports billed cost,
    which none of these do today.
  * Reaching the period cap pauses all new billable work (flag 'billable_paused'). Changing a cap does NOT
    clear it; only an explicit, recorded human resume does.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .contacts import ContactUnavailable
from .llm import LLMUnavailable
from .search import SearchUnavailable
from .store import Store, get_flag, set_flag
from .util import now_iso

BILLABLE_FLAG = "billable_paused"
PROVIDER_OPS = {"tavily": "search", "gemini": "generate", "hunter": "domain_search"}
UNIT_KIND = {"tavily": "requests", "gemini": "tokens", "hunter": "requests"}


class CostNotConfigured(RuntimeError):
    pass


class CostCapReached(SearchUnavailable, LLMUnavailable, ContactUnavailable):
    """Raised instead of making a billable call. Subclasses each provider's 'unavailable' error so existing
    per-call handling keeps known findings and records the gap instead of crashing."""

    def __init__(self, message: str, scope: str, reset_at: Optional[str] = None):
        super().__init__(message, "cost_cap")
        self.kind = "cost_cap"
        self.scope = scope            # company | period | unit_limit:<provider> | paused
        self.reset_at = reset_at


# ---------------------------------------------------------------- settings
def get_setting(store: Store, name: str) -> Dict[str, Any]:
    row = store.one("SELECT value_json FROM cost_settings WHERE name = %s", (name,))
    try:
        return json.loads(row["value_json"]) if row else {}
    except ValueError:
        return {}


def set_setting(store: Store, name: str, value: Dict[str, Any], by: str = "operator") -> None:
    """Record a cap/price change (with history). Never resumes paused billable work."""
    if name not in ("caps", "prices", "unit_limits"):
        raise ValueError("unknown cost setting")
    _validate(name, value)
    raw = json.dumps(value, sort_keys=True)
    with store.transaction():
        if store.one("SELECT name FROM cost_settings WHERE name = %s", (name,)):
            store.execute("UPDATE cost_settings SET value_json = %s, updated_at = %s, updated_by = %s WHERE name = %s",
                          (raw, now_iso(), by, name))
        else:
            store.execute("INSERT INTO cost_settings (name, value_json, updated_at, updated_by) VALUES (%s, %s, %s, %s)",
                          (name, raw, now_iso(), by))
        store.insert("cost_settings_history", {"name": name, "value_json": raw, "changed_at": now_iso(), "changed_by": by})


def _num(v: Any, what: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % what)
    if f < 0:
        raise ValueError("%s cannot be negative" % what)
    return f


def _validate(name: str, value: Dict[str, Any]) -> None:
    if name == "caps":
        for k in ("per_company_cap", "period_cap"):
            _num(value.get(k), k)
        if value.get("period", "month") not in ("month",):
            raise ValueError("period must be 'month' (calendar month, UTC)")
        cur = (value.get("currency") or "").strip()
        if value.get("per_company_cap") is not None and not cur:
            raise ValueError("currency is required with a cap")
    elif name == "prices":
        for k, p in value.items():
            if k not in PROVIDER_OPS:
                raise ValueError("unknown provider %s" % k)
            _num((p or {}).get("price"), "%s price" % k)
    else:
        for k, n in value.items():
            if k not in PROVIDER_OPS:
                raise ValueError("unknown provider %s" % k)
            _num(n, "%s unit limit" % k)


def readiness(store: Store, providers: List[str]) -> List[str]:
    """What is missing before paid research can run. Empty list = ready."""
    caps, prices, limits = get_setting(store, "caps"), get_setting(store, "prices"), get_setting(store, "unit_limits")
    out = []
    if caps.get("per_company_cap") is None:
        out.append("per-company research cost cap is not set")
    if caps.get("period_cap") is None:
        out.append("monthly cost cap is not set")
    for p in providers:
        if (prices.get(p) or {}).get("price") is None and limits.get(p) is None:
            out.append("%s: set a unit price or, if the price is unknown, a monthly %s limit" % (p, UNIT_KIND[p]))
    return out


def require_ready(store: Store, providers: List[str]) -> None:
    gaps = readiness(store, providers)
    if gaps:
        raise CostNotConfigured("cost limits must be set in Settings before paid research can run: " + "; ".join(gaps))
    paused = billable_paused(store)
    if paused:
        raise CostNotConfigured("billable work is paused (%s). A person must resume it in Controls; raising a cap "
                                "does not resume it" % paused)


# ---------------------------------------------------------------- period + pause
def period_key(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def period_reset(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")


def billable_paused(store: Store) -> Optional[str]:
    v = get_flag(store, BILLABLE_FLAG, "")
    return v or None


def pause_billable(store: Store, reason: str, actor: str = "system", actor_name: str = "cost meter") -> None:
    from .safety import record_control
    set_flag(store, BILLABLE_FLAG, reason[:400], actor_name)
    record_control(store, "billable", None, "pause", reason, actor, actor_name)


def resume_billable(store: Store, reason: str, by: str) -> None:
    """Explicit human action. Required even after a cap is raised or a new period starts."""
    from .safety import record_control
    if not (reason or "").strip():
        raise ValueError("a reason is required to resume billable work")
    set_flag(store, BILLABLE_FLAG, "", by)
    record_control(store, "billable", None, "resume", reason.strip(), "human", by)


# ---------------------------------------------------------------- spend
def spend(store: Store, period: Optional[str] = None, company_id: Optional[str] = None) -> Dict[str, Any]:
    period = period or period_key()
    where, params = "period_key = %s AND status IN ('reserved', 'settled')", [period]
    if company_id:
        where += " AND company_id = %s"
        params.append(company_id)
    rows = store.query("SELECT provider, cost_basis, est_cost, units, currency FROM cost_ledger WHERE " + where, params)
    by: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = by.setdefault(r["provider"], {"calls": 0, "units": 0.0, "estimated": 0.0, "unknown_price_calls": 0})
        b["calls"] += 1
        b["units"] += float(r["units"] or 0)
        if r["cost_basis"] == "unknown_price" or r["est_cost"] is None:
            b["unknown_price_calls"] += 1
        else:
            b["estimated"] += float(r["est_cost"])
    return {"period": period, "estimated_total": round(sum(b["estimated"] for b in by.values()), 6),
            "unknown_price_calls": sum(b["unknown_price_calls"] for b in by.values()), "by_provider": by,
            "currency": (get_setting(store, "caps").get("currency") or ""), "reset_at": period_reset()}


class Meter:
    """Per-company meter. One instance per company research/contact step."""

    def __init__(self, store: Store, run_id: Optional[str], company_id: Optional[str], now: Any = None):
        self.store, self.run_id, self.company_id = store, run_id, company_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.tripped: Optional[str] = None
        self.trip_message: Optional[str] = None

    def scope_label(self) -> str:
        return {"company": "per-company cap", "period": "monthly cap", "paused": "billable work paused"}.get(
            self.tripped or "", self.tripped or "")

    def _price(self, provider: str) -> Tuple[Optional[float], str]:
        p = get_setting(self.store, "prices").get(provider) or {}
        price = p.get("price")
        unit = UNIT_KIND[provider]
        if price is None:
            return None, unit
        price = float(price)
        return (price / 1_000_000.0 if unit == "tokens" else price), unit   # tokens priced per million

    def reserve(self, provider: str, est_units: float, model: Optional[str] = None) -> str:
        self._pending_pause = None
        try:
            return self._reserve(provider, est_units, model)
        except CostCapReached:
            if self._pending_pause:
                pause_billable(self.store, self._pending_pause)
            raise

    def _reserve(self, provider: str, est_units: float, model: Optional[str]) -> str:
        st = self.store
        now = self._now()
        pk, reset = period_key(now), period_reset(now)
        with st.transaction():
            # serialise reservations across processes: lock the caps row (Postgres); SQLite holds a write lock
            if st.dialect == "postgres":
                st.query("SELECT name FROM cost_settings WHERE name = 'caps' FOR UPDATE")
            paused = billable_paused(st)
            if paused:
                self._trip("paused", "billable work is paused: %s. A person must resume it in Settings" % paused, reset)
            caps = get_setting(st, "caps")
            limits = get_setting(st, "unit_limits")
            if caps.get("per_company_cap") is None or caps.get("period_cap") is None:
                self._trip("paused", "cost caps are not configured; set them in Settings", reset)
            unit_price, unit = self._price(provider)
            est = None if unit_price is None else round(est_units * unit_price, 8)
            if est is None:
                lim = limits.get(provider)
                if lim is None:
                    self._trip("unit_limit:" + provider, "%s price unknown and no unit limit set" % provider, reset)
                used = st.one("SELECT COALESCE(SUM(units), 0) AS u FROM cost_ledger WHERE period_key = %s AND provider = %s "
                              "AND cost_basis = 'unknown_price' AND status IN ('reserved', 'settled')", (pk, provider))
                if round(float(used["u"]) + est_units, 8) > round(float(lim), 8):
                    self._trip("unit_limit:" + provider, "%s monthly unit limit reached (%s %s); resets %s" % (
                        provider, _fmt(lim), unit, reset[:10]), reset)
            else:
                comp = st.one("SELECT COALESCE(SUM(est_cost), 0) AS c FROM cost_ledger WHERE company_id = %s "
                              "AND status IN ('reserved', 'settled')", (self.company_id,)) if self.company_id else {"c": 0}
                if round(float(comp["c"]) + est, 8) > round(float(caps["per_company_cap"]), 8):   # exact at the cap
                    self._trip("company", "per-company cost cap reached (%s %s)" % (_fmt(caps["per_company_cap"]),
                                                                                    caps.get("currency", "")), None)
                tot = st.one("SELECT COALESCE(SUM(est_cost), 0) AS c FROM cost_ledger WHERE period_key = %s "
                             "AND status IN ('reserved', 'settled')", (pk,))
                if round(float(tot["c"]) + est, 8) > round(float(caps["period_cap"]), 8):
                    msg = "monthly cost cap reached (%s %s); the budget period resets %s UTC" % (
                        _fmt(caps["period_cap"]), caps.get("currency", ""), reset[:16].replace("T", " "))
                    self._pending_pause = msg      # recorded after the rollback below
                    self._trip("period", msg, reset)
            return st.insert("cost_ledger", {
                "created_at": now_iso(), "period_key": pk, "run_id": self.run_id, "company_id": self.company_id,
                "provider": provider, "operation": PROVIDER_OPS[provider], "model": model, "units": est_units,
                "unit_kind": unit, "est_cost": est, "actual_cost": None, "currency": caps.get("currency"),
                "cost_basis": "estimated" if est is not None else "unknown_price", "status": "reserved",
                "detail": "reserved before call"})

    def _trip(self, scope: str, msg: str, reset: Optional[str]) -> None:
        self.tripped, self.trip_message = scope, msg
        raise CostCapReached(msg, scope, reset)

    def settle(self, ledger_id: str, provider: str, units: Optional[float], failed: bool = False,
               measured: bool = False) -> None:
        """Record the real units when known. A failed call keeps its reservation: the provider may still charge."""
        unit_price, _ = self._price(provider)
        row = self.store.one("SELECT units FROM cost_ledger WHERE id = %s", (ledger_id,)) or {}
        u = float(units if units is not None else (row.get("units") or 0))
        fields: Dict[str, Any] = {"status": "settled", "units": u}
        if unit_price is not None:
            fields["est_cost"] = round(u * unit_price, 8)
        fields["detail"] = ("call failed; provider may still charge" if failed else
                            ("units reported by provider" if measured else "units estimated (provider did not report usage)"))
        self.store.update("cost_ledger", ledger_id, fields)


def _fmt(v: Any) -> str:
    f = float(v)
    return ("%.4f" % f).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- metered client wrappers
class MeteredSearch:
    def __init__(self, inner: Any, meter: Meter):
        self.inner, self.meter = inner, meter
        self.calls = 0

    def search(self, query: str, **k: Any):
        lid = self.meter.reserve("tavily", 1)
        self.calls += 1
        try:
            out = self.inner.search(query, **k)
        except Exception:
            self.meter.settle(lid, "tavily", 1, failed=True)
            raise
        self.meter.settle(lid, "tavily", 1)
        return out


class MeteredLLM:
    def __init__(self, inner: Any, meter: Meter, model: Optional[str] = None):
        self.inner, self.meter = inner, meter
        self.model = model or getattr(inner, "model", None)

    def generate(self, prompt: str, json_mode: bool = True, max_tokens: int = 2048) -> str:
        est = len(prompt) / 4.0 + max_tokens       # conservative upper estimate; settled with reported usage
        lid = self.meter.reserve("gemini", est, self.model)
        try:
            out = self.inner.generate(prompt, json_mode=json_mode, max_tokens=max_tokens)
        except Exception:
            self.meter.settle(lid, "gemini", None, failed=True)
            raise
        usage = getattr(self.inner, "last_usage", None) or {}
        total = usage.get("totalTokenCount")
        self.meter.settle(lid, "gemini", float(total) if total is not None else None, measured=total is not None)
        return out


class MeteredHunter:
    def __init__(self, inner: Any, meter: Meter):
        self.inner, self.meter = inner, meter

    def domain_search(self, domain: str, limit: int = 10):
        lid = self.meter.reserve("hunter", 1)
        try:
            out = self.inner.domain_search(domain, limit=limit)
        except Exception:
            self.meter.settle(lid, "hunter", 1, failed=True)
            raise
        self.meter.settle(lid, "hunter", 1)
        return out


def billable_providers(deps: Any) -> List[str]:
    """Providers that would actually be called (a missing key means that step is skipped, not billed)."""
    from .contacts import NoHunter
    from .search import NoSearch
    out = []
    if deps.search is not None and not isinstance(deps.search, NoSearch):
        out.append("tavily")
    if deps.llm is not None:
        out.append("gemini")
    if deps.hunter is not None and not isinstance(deps.hunter, NoHunter):
        out.append("hunter")
    return out


def apply_config_defaults(store: Store, by: str = "config/costs.yaml") -> bool:
    """Load config/costs.yaml into the database if no caps are saved yet. Never overwrites saved limits
    (those are changed in Controls, with history). Returns True if defaults were applied."""
    if get_setting(store, "caps"):
        return False
    from .config import load_yaml
    try:
        cfg = load_yaml("costs.yaml")
    except OSError:
        return False
    if not cfg or cfg.get("per_company_cap") is None or cfg.get("period_cap") is None:
        return False
    set_setting(store, "caps", {"currency": cfg.get("currency", "USD"), "per_company_cap": float(cfg["per_company_cap"]),
                                "period_cap": float(cfg["period_cap"]), "period": "month"}, by)
    set_setting(store, "prices", {p: {"price": (None if v is None else float(v))} for p, v in (cfg.get("prices") or {}).items()}, by)
    set_setting(store, "unit_limits", {p: float(v) for p, v in (cfg.get("unit_limits") or {}).items() if v is not None}, by)
    return True
