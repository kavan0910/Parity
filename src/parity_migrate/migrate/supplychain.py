"""Supply-chain evidence per build run: lockfile hashes and a CycloneDX SBOM (docs/phase2-plan.md section 6).

    scan_workspace(ws, source=None) -> dict          # pure: lockfiles (sha256), components, unlocked manifests
    record_build(project, run_id, ws, source=None)   # writes runs/RUN-NNNN/{supply_chain.json, sbom.cdx.json}

Read from the build workspace AFTER the build job, never executed: only lockfiles and manifests are parsed
(package-lock.json, npm-shrinkwrap.json, yarn.lock, requirements*.txt, poetry.lock, Pipfile.lock, go.sum, Cargo.lock,
gradle.lockfile, and literal versions in pom.xml). Findings, all advisory warnings:

  * `unlocked`     a manifest (package.json, pyproject.toml, go.mod, Cargo.toml, build.gradle) with no lockfile;
  * `not committed` a lockfile that exists in the workspace but not in the source tree: the build generated it, so
                   the next build can resolve different versions (commit it for reproducible builds);
  * `unpinned`     requirements entries without `==` and pom.xml dependencies without a literal version.

`cutover` reports these for the new service's last build as warnings. The SBOM lists what the lockfiles name, not
what was downloaded: a mirror hash check is a separate control (the exec backend only allows the mirror hosts).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from .state import atomic_write_json, now_iso

IGNORE_DIRS = frozenset({".git", "node_modules", "target", "build", ".gradle", "__pycache__", ".venv", "venv",
                         ".deps", ".m2", "dist", ".cache", ".npm", "vendor", ".tox"})
MAX_FILE_BYTES = 8_000_000
MAX_COMPONENTS = 20_000
LOCK_NAMES = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "yarn.lock": "yarn", "poetry.lock": "poetry",
              "Pipfile.lock": "pipenv", "go.sum": "go", "Cargo.lock": "cargo", "gradle.lockfile": "gradle",
              "uv.lock": "uv", "pnpm-lock.yaml": "pnpm"}
# manifest -> lockfiles that satisfy it (in the same directory)
MANIFESTS = {"package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"),
             "pyproject.toml": ("poetry.lock", "uv.lock", "Pipfile.lock"),
             "go.mod": ("go.sum",), "Cargo.toml": ("Cargo.lock",),
             "build.gradle": ("gradle.lockfile",), "build.gradle.kts": ("gradle.lockfile",)}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _walk(root: Path):
    for dp, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(dp) / d).is_symlink())
        for n in sorted(names):
            p = Path(dp) / n
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def _comp(eco: str, name: str, version: str) -> dict:
    return {"ecosystem": eco, "name": name, "version": version}


# ------------------------------------------------------------------ parsers (text in, components out)
def parse_package_lock(text: str) -> list[dict]:
    data = json.loads(text)
    out = []
    pk = data.get("packages")
    if isinstance(pk, dict):
        for path, meta in pk.items():
            if not path or not isinstance(meta, dict):
                continue
            name = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
            if meta.get("version") and name:
                out.append(_comp("npm", name, str(meta["version"])))
    else:                                           # lockfileVersion 1
        def walk(deps):
            for name, meta in (deps or {}).items():
                if isinstance(meta, dict) and meta.get("version"):
                    out.append(_comp("npm", name, str(meta["version"])))
                    walk(meta.get("dependencies"))
        walk(data.get("dependencies"))
    return out


_YARN_ENTRY = re.compile(r'^"?((?:@[^/\s"]+/)?[^@\s"]+)@[^\n]*:\n(?:[ \t]+[^\n]*\n)*?[ \t]+version:? "?([^"\n]+)"?', re.M)


def parse_yarn_lock(text: str) -> list[dict]:
    return [_comp("npm", m.group(1), m.group(2).strip()) for m in _YARN_ENTRY.finditer(text)]


def parse_requirements(text: str) -> tuple[list[dict], list[str]]:
    out, unpinned = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9][A-Za-z0-9._+!-]*)", line)
        if m:
            out.append(_comp("pypi", m.group(1), m.group(3)))
        else:
            name = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if name:
                unpinned.append(name.group(1))
    return out, unpinned


def parse_toml_packages(text: str, eco: str) -> list[dict]:
    data = tomllib.loads(text)
    return [_comp(eco, p["name"], str(p["version"])) for p in data.get("package", [])
            if isinstance(p, dict) and p.get("name") and p.get("version")]


def parse_pipfile_lock(text: str) -> list[dict]:
    data = json.loads(text)
    out = []
    for section in ("default", "develop"):
        for name, meta in (data.get(section) or {}).items():
            v = str((meta or {}).get("version", "")).lstrip("=")
            if v:
                out.append(_comp("pypi", name, v))
    return out


def parse_go_sum(text: str) -> list[dict]:
    seen, out = set(), []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            ver = parts[1].removesuffix("/go.mod")
            if (parts[0], ver) not in seen:
                seen.add((parts[0], ver))
                out.append(_comp("golang", parts[0], ver))
    return out


def parse_gradle_lockfile(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^([^:#\s]+):([^:\s]+):([^=\s]+)=", line.strip())
        if m:
            out.append(_comp("maven", f"{m.group(1)}:{m.group(2)}", m.group(3)))
    return out


def parse_pom(text: str) -> tuple[list[dict], list[str]]:
    out, unpinned = [], []
    root = ET.fromstring(text)
    for dep in root.iter():
        if not dep.tag.endswith("dependency"):
            continue
        kids = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in dep}
        if not kids.get("artifactId"):
            continue
        name = f"{kids.get('groupId', '')}:{kids['artifactId']}"
        v = kids.get("version", "")
        if v and not v.startswith("${"):
            out.append(_comp("maven", name, v))
        else:
            unpinned.append(name)
    return out, unpinned


def purl(c: dict) -> str:
    eco, name, ver = c["ecosystem"], c["name"], c["version"]
    if eco == "maven" and ":" in name:
        g, a = name.split(":", 1)
        return f"pkg:maven/{g}/{a}@{ver}"
    if eco == "npm" and name.startswith("@"):
        return "pkg:npm/%40" + name[1:] + "@" + ver
    return f"pkg:{eco}/{name}@{ver}"


def cyclonedx(components: list[dict], *, name: str = "", created: str = "") -> dict:
    seen, comps = set(), []
    for c in components:
        k = (c["ecosystem"], c["name"], c["version"])
        if k in seen:
            continue
        seen.add(k)
        comps.append({"type": "library", "name": c["name"], "version": c["version"], "purl": purl(c),
                      "bom-ref": purl(c)})
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"timestamp": created or now_iso(), "component": {"type": "application", "name": name or "service"},
                         "tools": {"components": [{"type": "application", "name": "parity"}]}},
            "components": comps}


# ------------------------------------------------------------------ scan
def scan_workspace(ws: Path, source: Path | None = None) -> dict:
    """Pure read of lockfiles and manifests under `ws`. `source` (the tree the workspace was copied from) tells
    which lockfiles were generated by the build."""
    ws = Path(ws)
    files = {p.relative_to(ws).as_posix(): p for p in _walk(ws)}
    lockfiles, comps, unlocked, unpinned, errors = [], [], [], [], []
    for rel, p in files.items():
        base = p.name
        is_req = bool(re.fullmatch(r"requirements[\w.-]*\.txt", base))
        if base not in LOCK_NAMES and not is_req:
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            errors.append(f"{rel}: larger than {MAX_FILE_BYTES} bytes, not parsed")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            if base in ("package-lock.json", "npm-shrinkwrap.json"):
                got = parse_package_lock(text)
            elif base == "yarn.lock":
                got = parse_yarn_lock(text)
            elif is_req:
                got, unp = parse_requirements(text)
                unpinned += [f"{rel}: {n}" for n in unp]
            elif base in ("poetry.lock", "uv.lock"):
                got = parse_toml_packages(text, "pypi")
            elif base == "Cargo.lock":
                got = parse_toml_packages(text, "cargo")
            elif base == "Pipfile.lock":
                got = parse_pipfile_lock(text)
            elif base == "go.sum":
                got = parse_go_sum(text)
            elif base == "gradle.lockfile":
                got = parse_gradle_lockfile(text)
            else:
                got = []                                # pnpm-lock.yaml: hashed, not parsed
        except (ValueError, KeyError, tomllib.TOMLDecodeError) as e:
            errors.append(f"{rel}: not parsed ({type(e).__name__})")
            got = []
        committed = None if source is None else (Path(source) / rel).is_file()
        lockfiles.append({"path": rel, "kind": LOCK_NAMES.get(base, "pip"), "sha256": _sha256(p),
                          "components": len(got), "committed": committed})
        comps += got
    for rel, p in files.items():
        if p.name == "pom.xml" and p.stat().st_size <= MAX_FILE_BYTES:
            try:
                got, unp = parse_pom(p.read_text(encoding="utf-8", errors="replace"))
            except ET.ParseError:
                errors.append(f"{rel}: not parsed (xml)")
                continue
            comps += got
            unpinned += [f"{rel}: {n}" for n in unp]
            lockfiles.append({"path": rel, "kind": "maven-pom", "sha256": _sha256(p), "components": len(got),
                              "committed": None if source is None else (Path(source) / rel).is_file()})
    have = {f["path"] for f in lockfiles}
    for rel, p in files.items():
        wanted = MANIFESTS.get(p.name)
        if not wanted:
            continue
        d = Path(rel).parent
        if not any((d / n).as_posix() in have for n in wanted):
            if p.name == "pyproject.toml" and any(f["kind"] == "pip" and Path(f["path"]).parent == d for f in lockfiles):
                continue                                # pinned requirements.txt next to it counts
            unlocked.append(rel)
    notes = [f"{rel} has no lockfile: dependency versions are not reproducible" for rel in unlocked]
    notes += [f"{f['path']} was generated by the build, not found in the source: commit it"
              for f in lockfiles if f["committed"] is False]
    notes += [f"unpinned dependency: {u}" for u in unpinned[:50]]
    return {"schema_version": 1, "created": now_iso(), "lockfiles": lockfiles, "unlocked": unlocked,
            "unpinned": unpinned[:200], "components": comps[:MAX_COMPONENTS], "errors": errors, "warnings": notes,
            "truncated": len(comps) > MAX_COMPONENTS}


def record_build(project, run_id: str, ws: Path, source: Path | None = None, *, name: str = "") -> dict:
    """Scan the build workspace and write runs/RUN-NNNN/supply_chain.json (+ sbom.cdx.json). Returns the summary
    kept in harness/state.json: {lockfiles, unlocked, uncommitted, unpinned, components, warnings, sbom, report}."""
    from .paths import Phase2Layout
    lay = Phase2Layout(project)
    rd = lay.run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    rep = scan_workspace(ws, source)
    sbom = cyclonedx(rep["components"], name=name)
    atomic_write_json(rd / "sbom.cdx.json", sbom)
    full = {k: v for k, v in rep.items() if k != "components"} | {"components": len(sbom["components"]),
                                                                    "sbom": lay.rel(rd / "sbom.cdx.json")}
    atomic_write_json(rd / "supply_chain.json", full)
    return {"lockfiles": [{"path": f["path"], "sha256": f["sha256"], "committed": f["committed"]}
                          for f in rep["lockfiles"]],
            "unlocked": rep["unlocked"], "uncommitted": [f["path"] for f in rep["lockfiles"] if f["committed"] is False],
            "unpinned": len(rep["unpinned"]), "components": len(sbom["components"]), "warnings": rep["warnings"][:20],
            "sbom": full["sbom"], "report": lay.rel(rd / "supply_chain.json")}
