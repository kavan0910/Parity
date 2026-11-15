"""JSONPath subset used by `CharacterizationCase.normalize` masks, plus JSON-semantic equality and diffing.

Supported syntax (anything else raises MaskError):

    $                   the whole body
    $.a.b               object keys (identifier-like: letters, digits, `_`, `-`)
    $['a key']          quoted key (single or double quotes)
    $.items[0]          array index
    $.items[*].id       every array element
    $.meta.*            every value of an object

Header masks are written `headers.<name>` (lower-case header name) and are handled by compare.py.

Equality is JSON-semantic: object key order is irrelevant, `1 == 1.0`, but `true != 1` and `"1" != 1`;
array order matters.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable

MASK = "<masked>"
WILD = ("*",)

_IDENT = re.compile(r"[A-Za-z0-9_\-]+")
_TOKEN = re.compile(r"""\.(?P<key>[A-Za-z0-9_\-]+)|\.(?P<dotwild>\*)|\[(?P<idx>\d+)\]|\[(?P<wild>\*)\]"""
                    r"""|\[(?P<q>['"])(?P<qkey>(?:(?!(?P=q)).)*)(?P=q)\]""")

Token = tuple  # ("key", str) | ("index", int) | ("*",)


class MaskError(ValueError):
    pass


def parse(path: str) -> tuple[Token, ...]:
    """Tokens of a body mask. Raises MaskError for anything outside the subset."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise MaskError(f"mask must start with '$': {path!r}")
    if len(path) > 500:
        raise MaskError("mask too long")
    pos, out = 1, []
    while pos < len(path):
        m = _TOKEN.match(path, pos)
        if not m:
            raise MaskError(f"unsupported JSONPath syntax at {path[pos:]!r} in {path!r}")
        if m.group("key") is not None:
            out.append(("key", m.group("key")))
        elif m.group("qkey") is not None:
            out.append(("key", m.group("qkey")))
        elif m.group("idx") is not None:
            out.append(("index", int(m.group("idx"))))
        else:
            out.append(WILD)
        pos = m.end()
    return tuple(out)


def is_body_mask(mask: str) -> bool:
    return isinstance(mask, str) and mask.startswith("$")


def is_header_mask(mask: str) -> bool:
    return isinstance(mask, str) and mask.lower().startswith("headers.") and len(mask) > len("headers.")


def validate_mask(mask: str) -> str:
    """Return the canonical mask or raise MaskError."""
    if is_header_mask(mask):
        name = mask.split(".", 1)[1].strip().lower()
        if not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name):
            raise MaskError(f"invalid header mask {mask!r}")
        return f"headers.{name}"
    return format_path(parse(mask))


def format_path(tokens: Iterable[Token]) -> str:
    s = "$"
    for t in tokens:
        if t == WILD:
            s += "[*]"
        elif t[0] == "index":
            s += f"[{t[1]}]"
        elif _IDENT.fullmatch(t[1]):
            s += f".{t[1]}"
        else:
            q = '"' if "'" in t[1] else "'"
            s += f"[{q}{t[1]}{q}]"
    return s


def _mask(obj: Any, tokens: tuple[Token, ...]) -> Any:
    if not tokens:
        return MASK
    head, rest = tokens[0], tokens[1:]
    if head == WILD:
        if isinstance(obj, list):
            return [_mask(v, rest) for v in obj]
        if isinstance(obj, dict):
            return {k: _mask(v, rest) for k, v in obj.items()}
        return obj
    kind, val = head
    if kind == "key" and isinstance(obj, dict) and val in obj:
        return {**obj, val: _mask(obj[val], rest)}
    if kind == "index" and isinstance(obj, list) and 0 <= val < len(obj):
        out = list(obj)
        out[val] = _mask(obj[val], rest)
        return out
    return obj


def apply_masks(obj: Any, masks: Iterable[str]) -> Any:
    """Copy of `obj` with every value matched by a body mask replaced by MASK (header masks are ignored)."""
    out = copy.deepcopy(obj)
    for m in masks:
        if is_body_mask(m):
            out = _mask(out, parse(m))
    return out


def json_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def diff_paths(a: Any, b: Any, _prefix: tuple[Token, ...] = ()) -> list[tuple[Token, ...]]:
    """Concrete token paths where `a` and `b` differ (a length change marks the whole array)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[tuple[Token, ...]] = []
        for k in sorted(set(a) | set(b), key=str):
            p = _prefix + (("key", k),)
            if k not in a or k not in b:
                out.append(p)
            else:
                out += diff_paths(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff_paths(x, y, _prefix + (("index", i),))
        return out
    return [] if json_equal(a, b) else [_prefix]


def generalize(paths: Iterable[tuple[Token, ...]]) -> list[str]:
    """Masks from concrete diff paths: array indices become `[*]` (noise is per field, not per position)."""
    out = set()
    for p in paths:
        out.add(format_path(WILD if t[0] == "index" else t for t in p))
    return sorted(out)


def leaf_paths(obj: Any, _prefix: tuple[Token, ...] = ()):
    """(token path, value) for every scalar leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaf_paths(v, _prefix + (("key", k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaf_paths(v, _prefix + (("index", i),))
    else:
        yield _prefix, obj
