"""MCP server: lets Claude (desktop app, Cowork, Claude Code) drive hf on this machine.

Every generation costs money. Guards, all enforced server-side:
- hf_generate is a dry run unless dry_run=false is passed explicitly.
- Before any upload or POST, a reservation is written (and fsynced) to output/mcp_ledger.jsonl
  under a process-wide lock. An identical request (normalised: schema defaults applied, strings
  stripped, @paths resolved) reserved in the last 15 minutes is refused unless allow_duplicate=true,
  whether it is still being sent, was accepted, or ended in an uncertain state. If the ledger
  cannot be written, nothing is sent.
- Optional rolling 24 h cap on jobs launched through MCP (HFCLI_MCP_MAX_JOBS_PER_DAY; unset = no cap).
- Every dry run and submission carries a cost estimate when public pricing allows it; hf_spend
  reports launched/charged/estimated spend from the local history.
- hf_wait never blocks for more than ~45 s: polling has a hard deadline with short per-request
  timeouts, and downloading/Cloudinary run in a background worker (one per request_id).
The API key stays in ~/.higgsfield-cli/.env.local on this machine; tool output is redacted.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from hfcli import api, costs, jobs, storage
from hfcli.catalog import Target, describe_param, has_public_price, load, resolve, validate
from hfcli.config import Settings, UsageError, load_settings, redact
from hfcli.params import check_values, file_refs, with_placeholders

DUPLICATE_WINDOW = timedelta(minutes=15)
CAP_WINDOW = timedelta(hours=24)
WAIT_MAX_S = 35  # + at most one 10 s request => stays under a 60 s client timeout
REQUEST_TIMEOUT_S = 10
PREVIEW_PX = 512

INSTRUCTIONS = """Herramientas para generar imágenes y vídeos con la API de Higgsfield en el ordenador del usuario.
Cada generación cuesta dinero real. Flujo obligatorio:
1. Si dudas de los parámetros, hf_model_info(model).
2. hf_generate(..., dry_run=true) para validar sin gastar.
3. Enseña al usuario modelo, parámetros y coste estimado (estimated_cost_usd; si es null, dilo), y espera su OK explícito.
4. hf_generate(..., dry_run=false) devuelve un request_id.
5. hf_wait(request_id) hasta done=true; si devuelve done=false, vuelve a llamarlo.
Nunca repitas hf_generate porque una llamada tardó o falló: recupera con hf_wait(request_id).
Si submitted es "unknown" (incierto), no repitas: pide al usuario que revise open.higgsfield.ai/usage.
Archivos locales: "@/ruta/absoluta" en los campos de URL de params (image_url, image_urls, video_url...).
Para saber cuánto se lleva gastado: hf_spend.
"""

server = FastMCP("higgsfield", instructions=INSTRUCTIONS, log_level="WARNING")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

_guard = threading.Lock()          # serialises check-and-reserve across parallel tool calls
_finish_lock = threading.Lock()
_finishing: dict[str, Future] = {}
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hf-finish")


# ---------- helpers ----------

def _settings() -> Settings:
    return load_settings()


def _daily_cap() -> int | None:
    """Optional cap on MCP launches per rolling 24 h. Unset or invalid = no cap."""
    raw = os.getenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "").strip()
    try:
        return max(0, int(raw)) if raw else None
    except ValueError:
        return None


def _norm_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("@") and len(text) > 1:
            return "@" + str(Path(text[1:].strip()).expanduser().resolve())
        return text
    if isinstance(value, list):
        return [_norm_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm_value(v) for k, v in value.items()}
    return value


def normalise(target: Target, payload: dict) -> dict:
    """Canonical form for duplicate detection: schema defaults filled in, strings stripped, paths resolved."""
    out = {name: prop["default"] for name, prop in target.properties.items() if "default" in prop}
    out.update({k: _norm_value(v) for k, v in payload.items()})
    return out


def fingerprint(endpoint: str, params: dict) -> str:
    payload = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _aware(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.astimezone()  # naive timestamps are read as local time


def ledger_path(settings: Settings) -> Path:
    return settings.output_dir / "mcp_ledger.jsonl"


def _ledger_append(settings: Settings, record: dict) -> None:
    """Strict, durable append: raises OSError instead of silently losing a reservation."""
    path = ledger_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = redact(json.dumps({"ts": storage.now_iso(), **record}, ensure_ascii=False)) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _ledger(settings: Settings) -> list[dict]:
    """Reservations with their latest state: reserved, submitted, uncertain or released."""
    path = ledger_path(settings)
    if not path.exists():
        return []
    merged: dict[str, dict] = {}
    for raw in path.read_text("utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        key = rec.get("reservation")
        if key:
            first_ts = merged.get(key, {}).get("reserved_at") or rec.get("ts")
            merged[key] = {**merged.get(key, {}), **rec, "reserved_at": first_ts}
    return list(merged.values())


def _safe_ledger_update(settings: Settings, record: dict) -> None:
    try:
        _ledger_append(settings, record)
    except OSError:
        pass  # the reservation stays "reserved": duplicates remain blocked (fails closed)


def _history_record(settings: Settings, request_id: str) -> dict:
    for rec in storage.read_history(settings.history_file):
        if rec.get("request_id") == request_id:
            return rec
    return {}


def _previews(outputs: list[dict]) -> list[Image]:
    try:
        from PIL import Image as PILImage
    except ImportError:
        return []
    images = []
    for out in outputs[:4]:
        path = out.get("file")
        if not path or Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        try:
            with PILImage.open(path) as im:
                im.thumbnail((PREVIEW_PX, PREVIEW_PX))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=80)
            images.append(Image(data=buf.getvalue(), format="jpeg"))
        except OSError:
            continue
    return images


def _json(data: Any) -> str:
    return redact(json.dumps(data, ensure_ascii=False, indent=1))


# ---------- implementations (sync, testable) ----------

def models_impl(search: str = "", kind: str = "") -> str:
    cat = load()
    reverse = {v: k for k, v in cat["aliases"].items()}
    rows = []
    for endpoint, info in cat["endpoints"].items():
        alias = reverse.get(endpoint, endpoint)
        text = f"{alias} {endpoint} {info['title']}".lower()
        if (kind and info["output"] != kind) or (search and search.lower() not in text):
            continue
        rows.append(f"{alias} | {info['output']} | {endpoint} | {info['title']}")
    for name, preset in cat["presets"].items():
        if (not kind or kind == "image") and (not search or search.lower() in name):
            rows.append(f"{name} | image (workflow) | {preset['endpoint']} | {preset.get('note', '')}")
    return "alias | salida | endpoint | título\n" + "\n".join(rows)


def model_info_impl(model: str) -> str:
    target = resolve(model)
    if target.schema is None:
        return f"'{target.endpoint}' no está en el catálogo: se puede usar, pero sin validación de parámetros."
    schema = target.schema
    required = set(schema.get("required") or [])
    lines = [f"{target.label} → {target.endpoint}", f"{target.title} · salida: {target.output}"]
    if target.note:
        lines.append(f"Nota: {target.note}")
    if target.defaults:
        lines.append(f"Valores fijados por el alias: {json.dumps(target.defaults, ensure_ascii=False)}")
    lines.append("Precio (docs públicas, antes de descuentos): " +
                 (target.pricing if has_public_price(target) else "no publicado para este modelo"))
    lines.append("Parámetros:")
    lines += ["  " + describe_param(n, p, n in required) for n, p in schema.get("properties", {}).items()]
    if schema.get("allOf") or schema.get("if"):
        lines.append("Reglas condicionales: " + json.dumps(schema.get("allOf") or schema.get("if"), ensure_ascii=False))
    return "\n".join(lines)


def _reserve(settings: Settings, target: Target, fp: str, allow_duplicate: bool) -> tuple[str | None, dict | None]:
    """Check duplicates and the cap, then write a reservation. Returns (reservation, refusal)."""
    now = datetime.now().astimezone()
    with _guard:
        try:
            active = [r for r in _ledger(settings) if r.get("state") != "released"]
        except OSError as exc:
            return None, {"refused": "ledger_unreadable", "message": f"No puedo leer el registro de gasto: {exc}"}
        if not allow_duplicate:
            for rec in sorted(active, key=lambda r: str(r.get("reserved_at")), reverse=True):
                ts = _aware(rec.get("reserved_at"))
                if rec.get("fingerprint") == fp and ts and now - ts < DUPLICATE_WINDOW:
                    state = rec.get("state")
                    message = {
                        "reserved": "Una petición idéntica se está enviando ahora mismo. Espera y consulta hf_history.",
                        "submitted": "Esta misma petición ya se lanzó: usa hf_wait con ese request_id.",
                        "uncertain": "Una petición idéntica acabó en estado incierto (pudo cobrarse). "
                                     "Que el usuario revise open.higgsfield.ai/usage antes de repetir.",
                    }.get(state, "Petición idéntica reciente.")
                    return None, {"refused": "duplicate", "state": state, "request_id": rec.get("request_id"),
                                  "message": message + " Solo si el usuario quiere otra generación idéntica, "
                                                       "repite con allow_duplicate=true."}
        recent = [r for r in active if (_aware(r.get("reserved_at")) or now) > now - CAP_WINDOW]
        cap = _daily_cap()
        if cap is not None and len(recent) >= cap:
            return None, {"refused": "daily_limit", "launched_last_24h": len(recent), "limit": cap,
                          "message": "Límite de generaciones vía MCP en 24 h alcanzado (HFCLI_MCP_MAX_JOBS_PER_DAY "
                                     "en .env.local). No lo cambies sin que el usuario lo pida."}
        reservation = uuid.uuid4().hex[:12]
        try:
            _ledger_append(settings, {"reservation": reservation, "state": "reserved", "fingerprint": fp,
                                      "endpoint": target.endpoint, "model": target.label})
        except OSError as exc:
            return None, {"refused": "ledger_unwritable",
                          "message": f"No puedo anotar la reserva ({exc}); por seguridad no se envía nada."}
        return reservation, None


def generate_impl(model: str, prompt: str | None = None, params: dict | None = None,
                  dry_run: bool = True, allow_duplicate: bool = False) -> dict:
    settings = _settings()
    target = resolve(model)
    payload = {**target.defaults, **(params or {})}
    if prompt is not None:
        payload["prompt"] = prompt
    file_refs(payload)  # raises if a referenced local file is missing
    if target.schema is not None:
        errors = check_values(payload, target.schema) + validate(target, with_placeholders(payload))
        if errors:
            raise UsageError("Parámetros no válidos para " + target.endpoint + ": " + "; ".join(errors))
    est_usd, est_note = costs.estimate(target, payload)
    summary = {
        "model": target.label, "endpoint": target.endpoint, "params": payload,
        "validated": target.schema is not None,
        "estimated_cost_usd": est_usd, "cost_note": est_note,
        "pricing": target.pricing if has_public_price(target) else "precio no publicado para este modelo",
    }
    if dry_run:
        return {"dry_run": True, **summary,
                "next": "Nada enviado. Enseña esto al usuario y, si lo aprueba, repite con dry_run=false."}

    fp = fingerprint(target.endpoint, normalise(target, payload))
    reservation, refusal = _reserve(settings, target, fp, allow_duplicate)
    if refusal:
        return refusal
    res, status_url = jobs.submit(target, payload, settings, jobs.Options(quiet=True),
                                  history_extra={"via": "mcp", "fingerprint": fp, "reservation": reservation})
    if status_url is not None:
        _safe_ledger_update(settings, {"reservation": reservation, "state": "submitted", "request_id": res.request_id})
        return {"submitted": True, "request_id": res.request_id, "status": res.status, "model": target.label,
                "estimated_cost_usd": est_usd,
                "next": f"Llama a hf_wait(request_id='{res.request_id}') hasta que done=true."}
    if res.status == "uncertain":
        _safe_ledger_update(settings, {"reservation": reservation, "state": "uncertain"})
        return {"submitted": "unknown", "status": "uncertain", "error": res.error,
                "next": "No repitas. Que el usuario revise open.higgsfield.ai/usage."}
    _safe_ledger_update(settings, {"reservation": reservation, "state": "released"})  # rejected: nothing charged
    return {"submitted": False, "status": res.status, "error": res.error}


def _cached(known: dict) -> dict | None:
    outputs = known.get("outputs") or []
    if known.get("ok") and outputs and all(o.get("file") and Path(o["file"]).exists() for o in outputs):
        return {"done": True, "cached": True,
                **{k: known.get(k) for k in ("ok", "status", "request_id", "label", "endpoint", "outputs", "warnings")}}
    return None


def _finish(settings: Settings, known: dict, request_id: str, result: dict) -> jobs.JobResult:
    res = jobs.JobResult(ok=False, status="error", label=known.get("label", "result"),
                         endpoint=known.get("endpoint", "desconocido"), request_id=request_id,
                         params=known.get("params", {}))
    return jobs.complete(res, result, settings, jobs.Options(quiet=True), time.monotonic())


def wait_impl(request_id: str, max_seconds: int = WAIT_MAX_S) -> tuple[dict, list[dict]]:
    settings = _settings()
    status_url = api.status_url_for(request_id)  # validates the id
    budget = max(5, min(int(max_seconds), WAIT_MAX_S))
    deadline = time.monotonic() + budget
    with _finish_lock:
        fut = _finishing.get(request_id)
    if fut is None:
        known = _history_record(settings, request_id)
        cached = _cached(known)
        if cached:
            return cached, cached["outputs"]
        try:
            result = api.wait(status_url, timeout_s=budget, delay_s=3, request_timeout=REQUEST_TIMEOUT_S)
        except api.PollTimeout as exc:
            return {"done": False, "request_id": request_id, "status": exc.last or "in_progress",
                    "next": "Sigue en marcha. Vuelve a llamar a hf_wait con el mismo request_id."}, []
        except api.LostTrack as exc:
            hint = ("La API rechazó la consulta (clave no válida o request_id inexistente): revisa con hf_doctor."
                    if exc.code else "Error de red consultando el estado. Reintenta hf_wait en un rato.")
            return {"done": False, "request_id": request_id, "status": "unknown", "error": str(exc),
                    "next": hint + " No relances la generación."}, []
        with _finish_lock:  # one finisher per request, even if two hf_wait calls overlap
            fut = _finishing.get(request_id)
            if fut is None:
                fut = _pool.submit(_finish, settings, known, request_id, result)
                _finishing[request_id] = fut
    try:
        res = fut.result(timeout=max(1.0, deadline - time.monotonic()))
    except FutureTimeout:
        return {"done": False, "request_id": request_id, "status": "storing",
                "next": "Ya terminó en Higgsfield; se está descargando y copiando. Vuelve a llamar a hf_wait."}, []
    except Exception as exc:  # finishing crashed: clear it so the next call retries
        with _finish_lock:
            if _finishing.get(request_id) is fut:
                del _finishing[request_id]
        return {"done": False, "request_id": request_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}", "next": "Vuelve a llamar a hf_wait para reintentar."}, []
    with _finish_lock:
        if _finishing.get(request_id) is fut:
            del _finishing[request_id]
    return {"done": True, **res.to_dict()}, res.outputs


def history_impl(n: int = 10) -> list[dict]:
    keep = ("ts", "status", "ok", "label", "request_id", "via", "error", "warnings")
    records = storage.read_history(_settings().history_file)[-max(1, min(n, 100)):]
    out = []
    for rec in records:
        item = {k: rec.get(k) for k in keep if rec.get(k) is not None}
        item["outputs"] = [{k: o.get(k) for k in ("file", "cloudinary_url", "url") if o.get(k)}
                           for o in rec.get("outputs") or []]
        out.append(item)
    return out


def cancel_impl(request_id: str) -> str:
    try:
        api.cancel(request_id)
    except UsageError:
        raise
    except Exception as exc:  # 400 = already running or finished
        return redact(f"No se pudo cancelar (solo se puede mientras está en cola): {exc}")
    return f"Cancelación aceptada para {request_id}"


def spend_impl(days: float = 30) -> dict:
    return costs.spend_report(storage.read_history(_settings().history_file), max(0.01, min(float(days), 3650)))


def doctor_impl() -> str:
    from hfcli.cli import doctor_report
    lines, _ = doctor_report(_settings())
    cap = _daily_cap()
    lines.append("[INFO] Tope MCP: " + (f"{cap} generaciones cada 24 h" if cap is not None else
                                        "sin tope (opcional: HFCLI_MCP_MAX_JOBS_PER_DAY)"))
    return "\n".join(lines)


# ---------- MCP tools (async wrappers: blocking work runs in threads) ----------

def _error(exc: Exception) -> str:
    kind = "" if isinstance(exc, UsageError) else f"{type(exc).__name__}: "
    return redact(f"Error: {kind}{exc}")


@server.tool(annotations=READ_ONLY)
async def hf_models(search: str = "", kind: str = "") -> str:
    """Lista los modelos del catálogo (alias, tipo de salida, endpoint). kind: "video" o "image"."""
    try:
        return await anyio.to_thread.run_sync(models_impl, search, kind)
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=READ_ONLY)
async def hf_model_info(model: str) -> str:
    """Parámetros admitidos, valores permitidos, reglas y precio publicado de un modelo (alias o endpoint)."""
    try:
        return await anyio.to_thread.run_sync(model_info_impl, model)
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                         openWorldHint=True))
async def hf_generate(model: str, prompt: str | None = None, params: dict[str, Any] | None = None,
                      dry_run: bool = True, allow_duplicate: bool = False) -> str:
    """Valida (dry_run=true, por defecto, gratis) o lanza (dry_run=false, DE PAGO) una generación.
    model: alias (hf_models) o endpoint. params: parámetros del modelo; archivos locales como "@/ruta".
    Lanzar devuelve un request_id: después usa hf_wait. Nunca relances por un timeout."""
    try:
        return _json(await anyio.to_thread.run_sync(generate_impl, model, prompt, params, dry_run, allow_duplicate))
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True),
             structured_output=False)
async def hf_wait(request_id: str, max_seconds: int = WAIT_MAX_S, include_preview: bool = True) -> list:
    """Espera como mucho ~35-45 s a que termine una generación. Si done=false, vuelve a llamar.
    Al terminar descarga el resultado, lo copia a Cloudinary si está configurado y lo anota en el historial.
    Con include_preview devuelve miniaturas de las imágenes generadas."""
    try:
        data, outputs = await anyio.to_thread.run_sync(wait_impl, request_id, max_seconds)
    except Exception as exc:
        return [_error(exc)]
    content: list = [_json(data)]
    if include_preview and data.get("done") and data.get("ok"):
        content += await anyio.to_thread.run_sync(_previews, outputs)
    return content


@server.tool(annotations=READ_ONLY)
async def hf_history(n: int = 10) -> str:
    """Últimas generaciones (estado, request_id, archivos locales, URLs de Cloudinary)."""
    try:
        return _json(await anyio.to_thread.run_sync(history_impl, n))
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=READ_ONLY)
async def hf_spend(days: float = 30) -> str:
    """Gasto de los últimos N días por modelo: lanzadas, completadas, no cobradas (fallos/NSFW),
    pendientes o inciertas y USD estimado con precios públicos. El importe real está en open.higgsfield.ai/usage."""
    try:
        return _json(await anyio.to_thread.run_sync(spend_impl, days))
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def hf_cancel(request_id: str) -> str:
    """Cancela una generación que aún está en cola (si ya empezó, no se puede)."""
    try:
        return await anyio.to_thread.run_sync(cancel_impl, request_id)
    except Exception as exc:
        return _error(exc)


@server.tool(annotations=READ_ONLY)
async def hf_doctor() -> str:
    """Comprueba la configuración (clave, conexión, carpeta de salida, Cloudinary) sin gastar créditos."""
    try:
        return await anyio.to_thread.run_sync(doctor_impl)
    except Exception as exc:
        return _error(exc)


def serve() -> None:
    server.run("stdio")
