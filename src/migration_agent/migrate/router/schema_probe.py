"""Schema probe (runs inside the sandbox, test phase, no network): apply SQL to an ephemeral SQLite DB, dump schema.

    python schema_probe.py --sqlite /work/app/db.sqlite3 [--apply FILE ...] --out /reports/schema.json

Each --apply file runs as one script (`executescript`) in the given order, after the database file is opened
(created when missing). Output JSON:

    {"engine": "sqlite", "ok": true, "applied": [...], "error": "",
     "tables": {"book": {"columns": {"id": {"type": "INTEGER", "nullable": false, "pk": true}, ...}}},
     "triggers": [{"name": "...", "table": "..."}], "views": ["..."]}

Exit code 0 on success, 2 when a SQL file fails (the JSON still describes what was applied).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def introspect_sqlite(conn: sqlite3.Connection) -> dict:
    tables: dict[str, dict] = {}
    rows = conn.execute("SELECT name, type, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                        "ORDER BY name").fetchall()
    triggers, views = [], []
    for name, typ, tbl in rows:
        if typ == "table":
            cols = {}
            for _cid, cname, ctype, notnull, _dflt, pk in conn.execute(f'PRAGMA table_info("{name}")'):
                cols[cname] = {"type": ctype or "", "nullable": not notnull and not pk, "pk": bool(pk)}
            tables[name] = {"columns": cols}
        elif typ == "trigger":
            triggers.append({"name": name, "table": tbl})
        elif typ == "view":
            views.append(name)
    return {"tables": tables, "triggers": triggers, "views": views}


def run(db: str, apply: list[str]) -> tuple[int, dict]:
    out = {"engine": "sqlite", "ok": True, "applied": [], "error": ""}
    conn = sqlite3.connect(db)
    rc = 0
    try:
        for f in apply:
            try:
                with open(f, encoding="utf-8") as fh:
                    conn.executescript(fh.read())
                out["applied"].append(f)
            except (OSError, sqlite3.Error) as e:
                out["ok"] = False
                out["error"] = f"{f}: {type(e).__name__}: {e}"[:500]
                rc = 2
                break
        conn.commit()
        out.update(introspect_sqlite(conn))
    finally:
        conn.close()
    return rc, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ma schema probe")
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--apply", action="append", default=[])
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    rc, out = run(a.sqlite, a.apply)
    text = json.dumps(out, indent=2, sort_keys=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
