"""One generation end to end: upload local refs, submit, wait, then download / Cloudinary / history.

Rule: once a request may have been accepted (and billed), every outcome is recorded in the history and
reported with the request_id or with an explicit "uncertain" warning. Nothing after submit may escape.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import httpx
import higgsfield_client

from hfcli import EXIT_FAILED, EXIT_OK, EXIT_STORAGE, api, costs, storage
from hfcli.catalog import Target
from hfcli.config import Settings, redact
from hfcli.params import file_refs, replace_refs


@dataclass
class Options:
    download: bool = True
    cloud: bool = True
    timeout_min: float = 30.0
    poll_s: float = 3.0
    out_dir: Path | None = None
    quiet: bool = False


@dataclass
class JobResult:
    ok: bool
    status: str
    label: str
    endpoint: str
    request_id: str | None = None
    params: dict = field(default_factory=dict)
    outputs: list[dict] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    exit_code: int = EXIT_OK

    def to_dict(self) -> dict:
        return asdict(self)


class JobInterrupted(Exception):
    """Ctrl+C during a single job; carries the partial result so the CLI can still report it."""

    def __init__(self, result: JobResult):
        super().__init__(result.error)
        self.result = result


def log(msg: str, opts: Options | None = None) -> None:
    if opts and opts.quiet:
        return
    try:
        print(redact(msg), file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass  # a closed stderr must never interrupt a paid job


def upload_refs(params: dict, opts: Options, cache: dict[Path, str] | None = None) -> dict:
    cache = {} if cache is None else cache
    for path in file_refs(params):
        if path not in cache:
            log(f"Subiendo {path} ...", opts)
            cache[path] = api.upload_file(path)
    return replace_refs(params, cache)


def _http_code(exc: BaseException) -> int | None:
    response = getattr(exc.__cause__, "response", None)
    return getattr(response, "status_code", None)


def run(target: Target, params: dict, settings: Settings, opts: Options, prefix: str = "",
        upload_cache: dict[Path, str] | None = None, stop: threading.Event | None = None) -> JobResult:
    """Submit and wait until done (CLI). Nothing after the submit may escape without a request_id."""
    start = time.monotonic()
    res, status_url = submit(target, params, settings, opts, prefix, upload_cache, stop)
    if status_url is None:
        return res
    try:
        return finish(res, status_url, settings, opts, start, prefix, stop)
    except JobInterrupted:
        raise
    except Exception as exc:  # never lose a request_id that may already be billed
        res.status = "error"
        return _fail(res, f"error inesperado tras enviar ({type(exc).__name__}: {exc}). "
                          f"Recupera con `hf result {res.request_id}`", start, settings)


def submit(target: Target, params: dict, settings: Settings, opts: Options, prefix: str = "",
           upload_cache: dict[Path, str] | None = None, stop: threading.Event | None = None,
           history_extra: dict | None = None) -> tuple[JobResult, str | None]:
    """Upload local refs and send the request. Returns (result, status_url); status_url is None
    when nothing is left to wait for (rejected, uncertain or skipped)."""
    start = time.monotonic()
    res = JobResult(ok=False, status="error", label=target.label, endpoint=target.endpoint, params=params)
    if stop is not None and stop.is_set():
        res.status = "skipped"
        return _fail(res, "no enviado: el batch se interrumpió antes", start, settings), None
    est_usd, _ = costs.estimate(target, params)
    history_extra = {**(history_extra or {}), "est_usd": est_usd}
    try:
        res.params = params = upload_refs(params, opts, upload_cache)
    except (higgsfield_client.HiggsfieldClientError, httpx.HTTPError, OSError) as exc:
        return _fail(res, f"fallo subiendo archivos, no se ha enviado nada: {type(exc).__name__}: {exc}",
                     start, settings), None

    try:
        data = api.submit(target.endpoint, params)
    except higgsfield_client.HiggsfieldClientError as exc:
        code = _http_code(exc)
        if code is not None and code < 500 and code != 408:
            hint = " Baja --concurrency o espera." if code == 429 else ""
            return _fail(res, f"la API rechazó la petición (HTTP {code}): {exc}.{hint}", start, settings), None
        return _uncertain(res, f"HTTP {code}", start, settings, history_extra), None
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return _fail(res, f"sin conexión con la API, no se ha enviado nada ({type(exc).__name__})",
                     start, settings), None
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return _uncertain(res, type(exc).__name__, start, settings, history_extra), None
    except KeyboardInterrupt:
        _uncertain(res, "Ctrl+C durante el envío", start, settings, history_extra)
        res.status = "interrupted"
        raise JobInterrupted(res) from None

    res.request_id = str(data["request_id"])
    status_url = data.get("status_url") or ""
    if not api.is_higgsfield_url(status_url):
        status_url = api.status_url_for(res.request_id)
    res.status = "queued"
    log(f"{prefix}Enviado {target.label} request_id={res.request_id}", opts)
    storage.append_history(settings.history_file, {"event": "submitted", "request_id": res.request_id,
                                                   "label": target.label, "endpoint": target.endpoint,
                                                   "params": params, "status": "queued", **(history_extra or {})})
    return res, status_url


def finish(res: JobResult, status_url: str, settings: Settings, opts: Options, start: float,
           prefix: str = "", stop: threading.Event | None = None) -> JobResult:
    rid = res.request_id
    try:
        result = api.wait(status_url, timeout_s=opts.timeout_min * 60, delay_s=opts.poll_s,
                          on_status=lambda s: log(f"{prefix}  estado: {s}", opts), stop=stop)
    except api.PollTimeout as exc:
        res.status = "timeout"
        return _fail(res, f"{exc}. Sigue en Higgsfield: `hf result {rid}`", start, settings)
    except api.LostTrack as exc:
        res.status = "lost"
        return _fail(res, f"{exc}. Recupera con `hf result {rid}`", start, settings)
    except api.Interrupted as exc:
        res.status = "interrupted"
        return _fail(res, f"{exc}. Recupera con `hf result {rid}` o cancela con `hf cancel {rid}`", start, settings)
    except KeyboardInterrupt:
        res.status = "interrupted"
        _fail(res, f"interrumpido; la petición puede seguir en Higgsfield: `hf result {rid}` o `hf cancel {rid}`",
              start, settings)
        raise JobInterrupted(res) from None

    return complete(res, result, settings, opts, start)


def complete(res: JobResult, result: dict, settings: Settings, opts: Options, start: float) -> JobResult:
    """Turn a terminal status JSON into a JobResult: outputs, downloads, Cloudinary, history."""
    res.status = result.get("status", "unknown")
    if res.status != "completed":
        detail = {"failed": f"la generación falló: {result.get('error') or 'sin detalle'}",
                  "nsfw": "rechazada por moderación de contenido",
                  "canceled": "cancelada antes de procesarse"}.get(res.status, f"estado final inesperado '{res.status}'")
        return _fail(res, detail, start, settings)

    res.outputs = storage.extract_outputs(result)
    if not res.outputs:
        return _fail(res, "completada pero sin URL de salida en la respuesta", start, settings)
    res.ok = True
    _store(res, settings, opts)
    res.warnings = [redact(w) for w in res.warnings]
    res.exit_code = EXIT_STORAGE if res.warnings else EXIT_OK
    res.elapsed_s = round(time.monotonic() - start, 1)
    storage.append_history(settings.history_file, {"event": "final", **_summary(res)})
    return res


def _store(res: JobResult, settings: Settings, opts: Options) -> None:
    day = date.today().isoformat()
    out_dir = opts.out_dir or settings.output_dir / day
    multi = len(res.outputs) > 1
    for i, out in enumerate(res.outputs, 1):
        base = f"{res.label}_{res.request_id}" + (f"_{i}" if multi else "")
        if opts.download:
            try:
                out["file"] = str(storage.download(out["url"], out_dir, base, out.get("content_type")))
            except (httpx.HTTPError, OSError) as exc:
                res.warnings.append(f"descarga fallida ({out['url']}): {exc}")
        if opts.cloud and settings.cloudinary_configured:
            try:
                out["cloudinary_url"] = storage.cloudinary_upload(
                    out.get("file") or out["url"], folder=f"{settings.cloudinary_folder}/{day}", public_id=base,
                    tags=["higgsfield", res.label], context={"request_id": res.request_id, "endpoint": res.endpoint})
            except Exception as exc:  # Cloudinary raises its own error classes
                res.warnings.append(f"subida a Cloudinary fallida: {type(exc).__name__}: {exc}")


def _summary(res: JobResult) -> dict:
    keep = ("request_id", "label", "endpoint", "status", "ok", "outputs", "error", "warnings", "elapsed_s")
    return {k: v for k, v in res.to_dict().items() if k in keep}


def _fail(res: JobResult, message: str, start: float, settings: Settings) -> JobResult:
    res.ok, res.error, res.exit_code = False, redact(message), EXIT_FAILED
    res.elapsed_s = round(time.monotonic() - start, 1)
    if res.request_id:
        storage.append_history(settings.history_file, {"event": "final", **_summary(res)})
    return res


def _uncertain(res: JobResult, why: str, start: float, settings: Settings, extra: dict | None = None) -> JobResult:
    """The POST may or may not have been accepted: warn loudly and keep a trace of what was sent."""
    res.status = "uncertain"
    storage.append_history(settings.history_file, {"event": "submit_uncertain", "label": res.label,
                                                   "endpoint": res.endpoint, "params": res.params, "reason": why,
                                                   "status": "uncertain", **(extra or {})})
    return _fail(res, f"no se sabe si la petición llegó a Higgsfield ({why}). Revisa open.higgsfield.ai/usage "
                      f"antes de repetirla para no pagar dos veces", start, settings)
