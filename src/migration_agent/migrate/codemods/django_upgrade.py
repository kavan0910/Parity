"""django-upgrade + pyupgrade adapter (Django 3.2 -> 4.x/5.x, Python syntax upgrade), run in the sandbox.

Both tools are installed from the package mirror in the build phase (pinned versions). They exit 1 when they
rewrite a file, so the script ignores their exit codes; the orchestrator diffs the workspace instead.
"""
from __future__ import annotations

import re

DJANGO_UPGRADE = "django-upgrade==1.22.2"
PYUPGRADE = "pyupgrade==3.19.1"


def _ver(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


class DjangoUpgradeAdapter:
    name = "django_upgrade"
    image = "python:3.12-slim"

    def applies(self, spec) -> tuple[bool, str]:
        if spec.source.language.lower() != "python" or spec.target.language.lower() != "python":
            return False, "not a Python -> Python migration"
        if "django" not in (spec.target.framework or "").lower():
            return False, "target framework is not Django"
        return True, ""

    def command(self, spec) -> list[str] | str:
        dj = _ver((spec.target.framework or "").split("@", 1)[-1])
        if dj is None:
            return "django_upgrade: target.framework has no Django version (e.g. django@5.0)"
        py = _ver(spec.target.runtime) or (3, 10)
        files = "find . -name '*.py' -not -path './.venv/*' -not -path './node_modules/*' -print0"
        script = (f"pip install --no-cache-dir --disable-pip-version-check {DJANGO_UPGRADE} {PYUPGRADE} "
                  f"--target /tmp/tools >/dev/null && export PYTHONPATH=/tmp/tools PATH=/tmp/tools/bin:$PATH && "
                  f"({files} | xargs -0 -r python -m django_upgrade --target-version {dj[0]}.{dj[1]} || true) && "
                  f"({files} | xargs -0 -r python -m pyupgrade --py{py[0]}{py[1]}-plus || true)")
        return ["sh", "-c", script]
