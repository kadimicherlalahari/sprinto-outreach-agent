"""Actions shared by the CLI and web UI."""
from __future__ import annotations

from .config import Settings
from .contacts import manual_contact, verify_contact
from .fetch import FetchError
from .pipeline import build_deps, make_draft, save_contact
from .store import Store


def add_manual_contact(store: Store, settings: Settings, company_id: str, first: str, last: str, title: str,
                       email: str = None, note: str = "") -> str:
    co = store.one("SELECT * FROM companies WHERE id = %s", (company_id,))
    if not co:
        raise ValueError("company not found")
    if co["qualification"] != "qualified":
        raise ValueError("company is not qualified (%s); no draft will be created" % co["qualification"])
    deps = build_deps(settings)
    c = manual_contact(first, last, title, email, co["domain"], deps.icp, note)
    pages = []
    for path in ("", "about", "team", "leadership", "company"):
        try:
            pages.append(deps.fetcher.get("https://%s/%s" % (co["domain"], path)))
        except FetchError:
            continue
    c = verify_contact(c, co["name"], co["domain"], pages, deps.fetcher, deps.search, deps.sources)
    poc = save_contact(store, company_id, c)
    did = make_draft(store, settings, co["run_id"], company_id, co["name"], poc, deps)
    return "contact saved (verification: %s); draft %s" % (c.verification_status, did or "not created (no verified hook)")
