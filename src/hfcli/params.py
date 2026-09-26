"""Turn CLI input (prompt, key=value pairs, shortcut flags) into a request payload.

Local files are referenced as "@path" anywhere in a value; they are uploaded to Higgsfield
right before submitting and replaced by their public URL.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from hfcli.catalog import Target
from hfcli.config import UsageError

KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
PLACEHOLDER = "https://upload.pending/"
TRUE = {"true", "1", "yes", "si", "sí", "on"}
FALSE = {"false", "0", "no", "off"}


def _guess(value: str) -> Any:
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if not isinstance(parsed, str) else value


def coerce(key: str, value: str, prop: dict | None) -> Any:
    kind = (prop or {}).get("type")
    try:
        if kind == "string":
            return value
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            low = value.lower()
            if low in TRUE:
                return True
            if low in FALSE:
                return False
            raise ValueError
        if kind in ("array", "object") and value.strip()[:1] in "[{":
            return json.loads(value)
    except ValueError:
        raise UsageError(f"{key}: '{value}' no es un {kind} válido") from None
    if kind == "array":
        item_prop = (prop or {}).get("items")
        return [coerce(key, value, item_prop)]
    return _guess(value)


def split_inputs(items: list[str], props: dict | None = None) -> tuple[str | None, list[str]]:
    """Separate the free-text prompt from key=value items.

    With a known schema, `word=...` only counts as a parameter when `word` is one of the model's
    parameters, so a prompt like "Shot=wide, a cat" stays a prompt. A near-miss such as
    `durtion=10` is an error instead of being silently sent (and billed) without effect.
    """
    prompt, pairs = None, []
    for item in items:
        match = KV_RE.match(item)
        if match and (props is None or match.group(1) in props):
            pairs.append(item)
            continue
        if match and props is not None:
            close = difflib.get_close_matches(match.group(1), list(props), n=1, cutoff=0.75)
            if close or prompt is not None:
                hint = f" ¿Querías '{close[0]}'?" if close else ""
                raise UsageError(f"Parámetro desconocido '{match.group(1)}'.{hint} Mira `hf models <modelo>`.")
        if prompt is None:
            prompt = item
        else:
            raise UsageError("Solo se admite un prompt; ponlo entre comillas o usa --prompt.")
    return prompt, pairs


def uri_fields(schema: dict | None) -> set[str]:
    fields = set()
    for name, prop in (schema or {}).get("properties", {}).items():
        if prop.get("format") == "uri" or (prop.get("items") or {}).get("format") == "uri":
            fields.add(name)
    return fields


def check_values(params: dict, schema: dict | None) -> list[str]:
    """Checks the JSON Schema cannot express: unknown keys, URL-or-@file fields, stray @file refs."""
    if schema is None:
        return []
    props, uris, errors = schema.get("properties", {}), uri_fields(schema), []
    for key, value in params.items():
        if key not in props:
            close = difflib.get_close_matches(key, list(props), n=1, cutoff=0.75)
            errors.append(f"{key}: parámetro desconocido para este modelo" + (f" (¿{close[0]}?)" if close else ""))
            continue
        strings = [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]
        for text in strings:
            is_ref = text.startswith("@") and len(text) > 1
            if key in uris and not (is_ref or text.startswith(("https://", "http://"))):
                errors.append(f"{key}: '{text}' no es una URL ni @archivo (¿te falta la @?)")
            elif key not in uris and is_ref:
                errors.append(f"{key}: @archivo solo vale en campos de URL ({', '.join(sorted(uris)) or 'ninguno'})")
    return errors


def build_params(target: Target, prompt: str | None, pairs: list[str], shortcuts: dict) -> dict:
    props = target.properties
    params: dict[str, Any] = dict(target.defaults)
    if prompt is not None:
        params["prompt"] = prompt
    seen: set[str] = set()
    for item in pairs:
        key, raw = KV_RE.match(item).groups()
        prop = props.get(key)
        value = coerce(key, raw, prop)
        if (prop or {}).get("type") == "array" and key in seen:
            params[key] = list(params[key]) + list(value)  # repeated key appends
        else:
            params[key] = value
        seen.add(key)
    _apply_shortcuts(target, params, shortcuts)
    return params


def _apply_shortcuts(target: Target, params: dict, shortcuts: dict) -> None:
    props, known = target.properties, target.schema is not None
    scalar = {"ratio": "aspect_ratio", "res": "resolution", "duration": "duration", "seed": "seed"}
    for flag, key in scalar.items():
        value = shortcuts.get(flag)
        if value is None:
            continue
        if known and key not in props:
            raise UsageError(f"--{flag}: este modelo no tiene el parámetro '{key}' (mira `hf models {target.label}`).")
        params[key] = coerce(key, str(value), props.get(key))
    for flag in ("image", "video", "audio"):
        values = shortcuts.get(flag) or []
        if not values:
            continue
        single, plural = f"{flag}_url", f"{flag}_urls"
        if len(values) == 1 and (single in props or not known):
            params[single] = values[0]
        elif plural in props or (not known and len(values) > 1):
            params[plural] = list(params.get(plural, [])) + list(values)
        else:
            raise UsageError(f"--{flag}: el modelo no acepta {'varios archivos' if single in props else flag}. "
                             f"Mira `hf models {target.label}`.")


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    return obj


def file_refs(params: dict) -> list[Path]:
    found: list[Path] = []

    def visit(value: str) -> str:
        if value.startswith("@") and len(value) > 1:
            path = Path(value[1:]).expanduser()
            if not path.is_file():
                raise UsageError(f"No existe el archivo local {path}")
            if path not in found:
                found.append(path)
        return value

    _walk(params, visit)
    return found


def with_placeholders(params: dict) -> dict:
    """Copy with @file refs swapped for a URL so schema validation can run before uploading."""
    return _walk(params, lambda v: PLACEHOLDER + Path(v[1:]).name if v.startswith("@") and len(v) > 1 else v)


def replace_refs(params: dict, urls: dict[Path, str]) -> dict:
    return _walk(params, lambda v: urls[Path(v[1:]).expanduser()] if v.startswith("@") and len(v) > 1 else v)
