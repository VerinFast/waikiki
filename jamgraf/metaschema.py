"""Typed page metadata: a template declares the shape, pydantic checks it.

Frontmatter properties are (and stay) plain strings on disk — ``20 / 100`` is
written back exactly as typed. What a *template* can now declare is what those
strings are supposed to mean, so a mistyped value is visible instead of silent:

    hp: str                 # free text, no constraint beyond "present"
    blade*: int             # `*` = required, same marker custom elements use
    role: player | npc      # a choice (also written Literal["player", "npc"])
    born: date
    active: bool
    factions: list[str]     # comma-separated in the frontmatter

The declaration is a few lines of text, deliberately not a schema language: it
compiles to a ``pydantic`` ``TypeAdapter`` per field, so coercion and the error
messages are pydantic's, and the vocabulary is small enough to author in a
textarea. Per field rather than one generated model because metadata keys are
free text — ``Hit Points`` is a fine key and a terrible Python identifier.

Nothing here ever rejects a write. ``validate`` *reports*: a wiki is a place for
half-finished notes, and a page that fails its template's schema must still save
and still render (issue #28). Values are never rewritten on disk either — the
coerced ``values`` are handed to callers that want them, while the markdown the
human typed is left alone.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Literal, Optional

from pydantic import TypeAdapter, ValidationError

# Type names accepted in a declaration -> the python annotation validated against.
TYPES: dict[str, Any] = {
    "str": str, "string": str, "text": str,
    "int": int, "integer": int,
    "float": float, "number": float,
    "bool": bool, "boolean": bool,
    "date": _dt.date,
    "list": list[str], "list[str]": list[str], "list[int]": list[int],
}

_LITERAL = re.compile(r"^Literal\s*\[(.*)\]$", re.I)
_ADAPTERS: dict[Any, TypeAdapter] = {}


# --- Declaration --------------------------------------------------------------

def parse_schema(text: str) -> list[dict]:
    """``name[*]: type`` per line -> field dicts. Blank lines and `#` are skipped.

    Unknown type names degrade to ``str`` rather than raising: a typo in a
    template must not break the pages made from it.
    """
    fields: list[dict] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, _, spec = line.partition(":")
        name = name.strip()
        required = name.endswith("*")
        name = name.rstrip("*").strip()
        spec = spec.split("#", 1)[0]        # trailing comment
        spec = spec.split("=", 1)[0].strip()  # `= default` — declares optional
        if not name or not spec:
            continue
        norm = name.replace(" ", "").lower()
        if norm in seen:
            continue                        # first declaration of a key wins
        seen.add(norm)
        choices = _choices(spec)
        ftype = "" if choices else _typename(spec)
        fields.append({"name": name, "type": ftype, "required": required,
                       "choices": choices})
    return fields


def _choices(spec: str) -> list[str]:
    m = _LITERAL.match(spec)
    if m:
        return [c.strip().strip("\"'") for c in m.group(1).split(",") if c.strip()]
    if "|" in spec:
        return [c.strip() for c in spec.split("|") if c.strip()]
    return []


def _typename(spec: str) -> str:
    key = spec.replace(" ", "").lower()
    return key if key in TYPES else "str"


def describe(field: dict) -> str:
    """One-line human description of a field, for the editor's hints."""
    what = " | ".join(field["choices"]) if field["choices"] else field["type"]
    return f"{what} (required)" if field["required"] else what


# --- Validation ---------------------------------------------------------------

def _adapter(field: dict) -> TypeAdapter:
    if field["choices"]:
        ann: Any = Literal[tuple(field["choices"])]        # type: ignore[valid-type]
    else:
        ann = TYPES.get(field["type"], str)
    key = str(ann)
    if key not in _ADAPTERS:
        _ADAPTERS[key] = TypeAdapter(ann)
    return _ADAPTERS[key]


def _prepare(text: str, field: dict) -> Any:
    """Frontmatter is text; a list type reads it as comma-separated."""
    if field["type"].startswith("list"):
        return [p.strip() for p in text.split(",") if p.strip()]
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value


def _message(exc: ValidationError) -> str:
    errs = exc.errors()
    return errs[0].get("msg", "invalid value") if errs else "invalid value"


def validate(props: dict, schema_text: str) -> dict:
    """Check a page's properties against a declaration. Never raises, never
    rewrites: returns ``ok`` plus per-key ``errors`` and the coerced ``values``.

    Keys match the way ``store.set_properties`` matches them — case- and
    space-insensitively — so ``Hit Points`` satisfies a ``hitpoints`` field.
    Properties the schema doesn't mention are left alone: a template describes
    what a page is expected to carry, not everything it may carry.
    """
    fields = parse_schema(schema_text)
    lookup = {str(k).replace(" ", "").lower(): k for k in (props or {})}
    errors: list[dict] = []
    values: dict[str, Any] = {}
    for field in fields:
        key = lookup.get(field["name"].replace(" ", "").lower())
        raw: Optional[str] = None if key is None else props.get(key)
        text = "" if raw is None else str(raw).strip()
        if not text:
            values[field["name"]] = None
            if field["required"]:
                errors.append({
                    "key": key or field["name"],
                    "expected": describe(field),
                    "message": ("required by the template, but empty"
                                if key else "required by the template, but missing"),
                })
            continue
        try:
            values[field["name"]] = _jsonable(
                _adapter(field).validate_python(_prepare(text, field)))
        except ValidationError as exc:
            values[field["name"]] = None
            errors.append({"key": key, "expected": describe(field),
                           "message": _message(exc)})
    return {"ok": not errors, "errors": errors, "fields": fields, "values": values}
