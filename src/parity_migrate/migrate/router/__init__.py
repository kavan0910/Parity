"""Strangler router and schema probe image (docs/phase2-plan.md sections 3 and 9, milestone M6).

Package data copied into `<project>/harness/router/` and built with `docker build --network none` by
`parity harness build PROJECT --composite` (or lazily by `config schema-check`):

    Dockerfile       python (stdlib only) image, non-root, no RUN steps
    proxy.py         reverse proxy: routes each flipped slice's endpoints to the new service, everything else to
                     legacy; reloads /routes/routes.json when it changes (flips apply without a restart)
    schema_probe.py  applies SQL files to an ephemeral SQLite database and dumps its schema as JSON

Both scripts are standard-library only and are importable for unit tests (`proxy.RouteTable`,
`schema_probe.introspect_sqlite`). The orchestrator never runs them on the host.
"""
from __future__ import annotations

from pathlib import Path

ROUTER_DIR = Path(__file__).parent
ASSETS = ("Dockerfile", "proxy.py", "schema_probe.py")
ROUTER_VERSION = "1"
