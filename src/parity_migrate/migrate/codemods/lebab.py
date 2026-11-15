"""JavaScript modernisation adapter (lebab): deterministic ES5 -> ES6+ rewrites for same-language upgrades, run in
the sandbox. Replaces the earlier jscodeshift stub: the npm `js-codemod` package ships no usable transforms, while
`lebab` is a maintained tool whose transforms are designed to be safe.

Selected transforms: `let` (var -> let/const), `template` (string concatenation -> template literals) and
`obj-shorthand`. Everything else, and anything that is not JavaScript -> JavaScript (TypeScript, framework or
language moves such as Express to Fastify or JS to Go), is left to the model: the adapter then reports itself as
skipped with a reason. The characterization suite, not this adapter, decides whether behaviour moved.

The tool is installed from the package mirror in the build phase (pinned) under /tmp, which is mounted noexec in the
sandbox, so it is started through `node`. A file lebab cannot parse is reported on stderr and fails the job (the
adapter never hides a failed transform).
"""
from __future__ import annotations

LEBAB = "lebab@3.2.4"
TRANSFORMS = ("let", "template", "obj-shorthand")
BIN = "/tmp/tools/node_modules/lebab/bin/index.js"


class LebabAdapter:
    name = "lebab"
    image = "node:20-alpine"

    def applies(self, spec) -> tuple[bool, str]:
        if spec.source.language.lower() not in ("javascript", "typescript"):
            return False, "not a JavaScript/TypeScript source"
        return True, ""

    def command(self, spec) -> list[str] | str:
        if spec.source.language.lower() != "javascript" or spec.target.language.lower() != "javascript":
            return "lebab: only JavaScript -> JavaScript upgrades have deterministic transforms (the model does this move)"
        if spec.kind == "framework_move" and (spec.source.framework or "").lower() != (spec.target.framework or "").lower():
            return "lebab: no deterministic transform for a framework move (the model does this move)"
        tr = ",".join(TRANSFORMS)
        script = (
            "export HOME=/tmp npm_config_cache=/tmp/.npm && rc=0 && "          # read-only rootfs, non-root: no /.npm
            f"npm install --no-audit --no-fund --no-save --prefix /tmp/tools {LEBAB} >/dev/null && "
            "find . -type f \\( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' \\) -not -path '*/node_modules/*' "
            "-not -path './.git/*' -print | while IFS= read -r f; do "
            f"node {BIN} --replace \"$f\" --transform {tr} >/dev/null 2>/tmp/lebab.err || "
            "{ echo \"lebab failed on $f: $(head -c 300 /tmp/lebab.err)\" >&2; echo FAIL >> /tmp/lebab.fail; }; done; "
            "[ ! -s /tmp/lebab.fail ] || exit 1")
        return ["sh", "-c", script]


# the previous public name, kept so existing imports keep working
JscodeshiftAdapter = LebabAdapter
