"""Compare an observed response with an expectation: status, allowlisted headers, JSON-semantic body with masks."""
from __future__ import annotations

from typing import Any, Iterable

from ..models import Expectation
from .jsonpath import apply_masks, diff_paths, format_path, is_header_mask, json_equal
from .models import FieldDiff, Observed

MAX_DIFFS = 50


def norm_header(name: str, value: str) -> str:
    v = " ".join(str(value).split())
    if name == "content-type":
        return ";".join(p.strip() for p in v.lower().split(";") if p.strip())
    return v


def filter_headers(headers: dict[str, str], allowlist: Iterable[str]) -> dict[str, str]:
    allow = {h.lower() for h in allowlist}
    return {k.lower(): norm_header(k.lower(), v) for k, v in headers.items() if k.lower() in allow}


def header_masks(masks: Iterable[str]) -> set[str]:
    return {m.split(".", 1)[1].lower() for m in masks if is_header_mask(m)}


def compare_body(expected: Any, actual: Any, masks: Iterable[str]) -> list[FieldDiff]:
    masks = list(masks)
    e, a = apply_masks(expected, masks), apply_masks(actual, masks)
    if json_equal(e, a):
        return []
    if isinstance(e, (dict, list)) and isinstance(a, (dict, list)):
        out = []
        for p in diff_paths(e, a)[:MAX_DIFFS]:
            out.append(FieldDiff(field=f"body.{format_path(p)}", expected=_at(e, p), actual=_at(a, p)))
        return out
    return [FieldDiff(field="body", expected=e, actual=a)]


def _at(obj: Any, path) -> Any:
    cur = obj
    for t in path:
        try:
            cur = cur[t[1]]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def compare(expect: Expectation | dict, observed: Observed, normalize: Iterable[str],
            header_allowlist: Iterable[str]) -> list[FieldDiff]:
    """Field diffs between an expectation (or a `legacy_observed`-style dict) and an observed response.
    A `specified` expectation is compared on status, the headers it names, and the body."""
    if isinstance(expect, dict):
        status, headers, body = expect.get("status"), expect.get("headers") or {}, expect.get("body")
        specified = False
    else:
        status, headers, body = expect.status, expect.headers, expect.body
        specified = expect.source == "specified"
    normalize = list(normalize)
    out: list[FieldDiff] = []
    if status != observed.status:
        out.append(FieldDiff(field="status", expected=status, actual=observed.status))
    hm = header_masks(normalize)
    exp_h = filter_headers(headers, header_allowlist)
    act_h = filter_headers(observed.headers, header_allowlist)
    # recorded: every allowlisted header on either side counts (a new header is a difference);
    # specified: only the headers the approved spec names
    names = set(exp_h) if specified else set(exp_h) | set(act_h)
    for name in sorted(names - hm):
        if exp_h.get(name) != act_h.get(name):
            out.append(FieldDiff(field=f"headers.{name}", expected=exp_h.get(name), actual=act_h.get(name)))
    out += compare_body(body, observed.body, normalize)
    return out
