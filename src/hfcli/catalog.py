"""Model catalog: endpoints with their official input JSON Schema, short aliases and workflow presets."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from hfcli.config import UsageError


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(resources.files("hfcli").joinpath("catalog.json").read_text("utf-8"))


@dataclass
class Target:
    endpoint: str
    label: str                     # used in file names and history
    schema: dict | None            # None = endpoint not in the catalog (no validation)
    title: str = ""
    output: str = ""               # "image" | "video" | ""
    defaults: dict = field(default_factory=dict)
    note: str | None = None
    pricing: str = ""

    @property
    def properties(self) -> dict:
        return (self.schema or {}).get("properties", {})


def _slug(endpoint: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", endpoint.lower()).strip("-")


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def resolve(name: str) -> Target:
    cat = load()
    name = name.strip().strip("/")
    if not SAFE_NAME.match(name) or ".." in name:
        # never let a "model" become an absolute URL: the API key would be sent to that host
        raise UsageError(f"Nombre de modelo no válido: {name!r}. Usa un alias o un endpoint como 'vendor/model/variant'.")
    if name in cat["presets"]:
        preset = cat["presets"][name]
        ep = cat["endpoints"][preset["endpoint"]]
        return Target(preset["endpoint"], name, ep["schema"], ep["title"], ep["output"],
                      dict(preset["defaults"]), preset.get("note"), ep.get("pricing", ""))
    endpoint = cat["aliases"].get(name, name)
    if endpoint in cat["endpoints"]:
        ep = cat["endpoints"][endpoint]
        reverse = {v: k for k, v in cat["aliases"].items()}
        return Target(endpoint, reverse.get(endpoint, _slug(endpoint)), ep["schema"], ep["title"], ep["output"],
                      pricing=ep.get("pricing", ""))
    if "/" not in endpoint:
        close = [a for a in list(cat["aliases"]) + list(cat["presets"]) if name.split("-")[0] in a]
        hint = f" ¿Querías: {', '.join(sorted(close)[:6])}?" if close else ""
        raise UsageError(f"Modelo desconocido: '{name}'. Usa `hf models` para ver alias.{hint}")
    return Target(endpoint, _slug(endpoint), None)  # uncatalogued endpoint: sent as-is


def validate(target: Target, params: dict) -> list[str]:
    """Validate against the official schema. Returns human-readable errors (empty = valid)."""
    if target.schema is None:
        return []
    validator = Draft202012Validator(target.schema)
    errors = []
    for err in sorted(validator.iter_errors(params), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in err.path) or "(petición)"
        errors.append(f"{where}: {err.message}")
    return errors


def has_public_price(target: Target) -> bool:
    return bool(target.pricing) and "not available" not in target.pricing


def describe_param(name: str, prop: dict, required: bool) -> str:
    kind = prop.get("type", "any")
    if kind == "array":
        kind = f"array[{prop.get('items', {}).get('type', 'any')}]"
    parts = [f"{name:<22} {kind:<15} {'obligatorio' if required else 'opcional':<12}"]
    if "default" in prop:
        parts.append(f"def={json.dumps(prop['default'], ensure_ascii=False)}")
    if "enum" in prop:
        parts.append("valores=" + "|".join(str(v) for v in prop["enum"]))
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if lo is not None or hi is not None:
        parts.append(f"rango={lo if lo is not None else ''}..{hi if hi is not None else ''}")
    if kind.startswith("array") and ("minItems" in prop or "maxItems" in prop):
        parts.append(f"items={prop.get('minItems', 0)}..{prop.get('maxItems', '')}")
    if "maxLength" in prop:
        parts.append(f"max_chars={prop['maxLength']}")
    return "  ".join(parts)
