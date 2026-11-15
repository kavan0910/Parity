"""jscodeshift adapter (stub). Express -> Fastify / JS -> TS transforms are not selected yet; the adapter
reports itself as skipped so the model handles these moves (framework moves arrive in M6)."""
from __future__ import annotations


class JscodeshiftAdapter:
    name = "jscodeshift"
    image = "node:20-alpine"

    def applies(self, spec) -> tuple[bool, str]:
        if spec.source.language.lower() not in ("javascript", "typescript"):
            return False, "not a JavaScript/TypeScript source"
        return True, ""

    def command(self, spec) -> list[str] | str:
        return "jscodeshift: no transforms selected yet (stub; TODO M6)"
