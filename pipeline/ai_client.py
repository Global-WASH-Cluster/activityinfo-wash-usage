"""Thin ActivityInfo REST client for the HPC Country Module review.

Auth: bearer token in ~/.config/activityinfo/token (chmod 600), or the
ACTIVITYINFO_TOKEN env var. Base URL is the HPC tools deployment.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

BASE_URL = "https://tools.humanitarianaction.info"
TOKEN_FILE = Path.home() / ".config" / "activityinfo" / "token"


def _load_token() -> str:
    tok = os.environ.get("ACTIVITYINFO_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise SystemExit(
        f"No token. Put it in {TOKEN_FILE} (chmod 600) or set ACTIVITYINFO_TOKEN."
    )


class Client:
    def __init__(self, base_url: str = BASE_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token or _load_token()}",
                "Accept": "application/json",
            }
        )

    def get(self, path: str, _timeout: int = 60, **params):
        for attempt in range(4):
            r = self.s.get(self.base_url + path, params=params or None, timeout=_timeout)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def query(self, form_id: str, columns: dict[str, str], _timeout: int = 120):
        """POST analysis query. columns maps output id -> ActivityInfo expression.

        Returns {offset, rows, totalRows, columns:{id:{type,values}}}. Selecting
        few columns avoids the full reference expansion of GET /query/columns.
        """
        body = {
            "rowSources": [{"rootFormId": form_id}],
            "columns": [{"id": k, "expression": v} for k, v in columns.items()],
        }
        for attempt in range(4):
            r = self.s.post(
                self.base_url + "/resources/query/columns", json=body, timeout=_timeout
            )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def count(self, form_id: str) -> int:
        return self.query(form_id, {"id": "_id"}).get("totalRows", 0)

    # --- endpoints (verify shapes against a live response before trusting) ---

    def whoami(self):
        return self.get("/resources/users/self")

    def databases(self):
        return self.get("/resources/databases")

    def database(self, db_id: str):
        return self.get(f"/resources/databases/{db_id}")

    def form_schema(self, form_id: str):
        return self.get(f"/resources/form/{form_id}/schema")

    def form_query_columns(self, form_id: str, **params):
        return self.get(f"/resources/form/{form_id}/query/columns", **params)

    def form_record_count(self, form_id: str):
        return self.get(f"/resources/form/{form_id}/record/count")


if __name__ == "__main__":
    c = Client()
    try:
        me = c.whoami()
        print("authenticated as:", me.get("email") or me.get("userId") or me)
    except requests.HTTPError as e:
        # a read-only token cannot hit /users/self; not fatal for this review
        print("whoami skipped:", e.response.status_code, e.response.json().get("message"))
    dbs = c.databases()
    items = dbs if isinstance(dbs, list) else dbs.get("databases", dbs)
    print(f"{len(items)} databases visible:")
    for d in items:
        print(" ", d.get("databaseId", d.get("id")), "-", d.get("label", d.get("name")))
