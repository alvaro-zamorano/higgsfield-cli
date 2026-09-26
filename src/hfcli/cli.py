"""Entry point for the `hf` command."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx
import higgsfield_client

from hfcli import EXIT_FAILED, EXIT_OK, EXIT_STORAGE, EXIT_USAGE, __version__, api, costs, jobs, storage
from hfcli.catalog import Target, describe_param, has_public_price, load, resolve, validate
from hfcli.config import Settings, UsageError, credential_state, load_settings, redact
from hfcli.params import build_params, check_values, file_refs, split_inputs, with_placeholders


# ---------- shared helpers ----------

def _options(args) -> jobs.Options:
    return jobs.Options(download=not args.no_download, cloud=not args.no_cloud, timeout_min=args.timeout,
                        poll_s=args.poll, out_dir=Path(args.out).expanduser() if args.out else None,
                        quiet=getattr(args, "quiet", False))


def _check(target: Target, params: dict, skip_validation: bool, where: str = "") -> None:
    file_refs(params)  # raises if a referenced local file is missing
    if target.schema is None:
        print(f"{where}Aviso: '{target.endpoint}' no está en el catálogo; se envía sin validar.", file=sys.stderr)
        return
    if skip_validation:
        return
    errors = check_values(params, target.schema) + validate(target, with_placeholders(params))
    if errors:
        lines = "\n  - ".join(errors)
        raise UsageError(f"{where}Parámetros no válidos para {target.endpoint}:\n  - {lines}\n"
                         f"Consulta `hf models {target.label}`. Si el catálogo estuviera desactualizado: --no-validate.")


def _report(res: jobs.JobResult, as_json: bool) -> None:
    try:
        _print_result(res, as_json)
    except (BrokenPipeError, OSError):
        pass  # the job outcome is in the history; the exit code still reflects it


def _print_result(res: jobs.JobResult, as_json: bool) -> None:
    if as_json:
        print(redact(json.dumps(res.to_dict(), ensure_ascii=False)), flush=True)
        return
    if res.ok:
        print(f"OK {res.label} · request_id={res.request_id} · {res.elapsed_s}s")
        for out in res.outputs:
            print(f"  url:        {out['url']}")
            if out.get("file"):
                print(f"  archivo:    {out['file']}")
            if out.get("cloudinary_url"):
                print(f"  cloudinary: {out['cloudinary_url']}")
        for warning in res.warnings:
            print(f"  AVISO: {warning}")
    else:
        rid = f" · request_id={res.request_id}" if res.request_id else ""
        print(f"FALLO {res.label} [{res.status}]{rid}: {res.error}")


def _add_storage_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-download", action="store_true", help="no descargar los archivos")
    p.add_argument("--no-cloud", action="store_true", help="no subir a Cloudinary aunque esté configurado")
    p.add_argument("--out", help="carpeta de descarga (por defecto output/AAAA-MM-DD)")
    p.add_argument("--timeout", type=_bounded(1, 240), default=30.0, help="minutos máximos de espera (1-240, def. 30)")
    p.add_argument("--poll", type=_bounded(1, 60), default=3.0, help="segundos entre consultas (1-60, def. 3)")
    p.add_argument("--json", action="store_true", help="resultado final como JSON en stdout")


def _bounded(lo: float, hi: float):
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' no es un número") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"debe estar entre {lo:g} y {hi:g}")
        return value
    return parse


def _int_bounded(lo: int, hi: int):
    def parse(text: str) -> int:
        value = _bounded(lo, hi)(text)
        if value != int(value):
            raise argparse.ArgumentTypeError("debe ser un entero")
        return int(value)
    return parse


def _run_single(target: Target, params: dict, settings: Settings, opts: jobs.Options, as_json: bool) -> int:
    try:
        res = jobs.run(target, params, settings, opts)
    except jobs.JobInterrupted as exc:
        _report(exc.result, as_json)
        return EXIT_FAILED
    _report(res, as_json)
    return res.exit_code


# ---------- commands ----------

def cmd_gen(args, settings: Settings) -> int:
    target = resolve(args.model)
    prompt, pairs = split_inputs(args.inputs, target.properties if target.schema is not None else None)
    if args.prompt is not None:
        if prompt is not None:
            raise UsageError("Prompt duplicado: usa el posicional o --prompt, no ambos.")
        prompt = args.prompt
    shortcuts = {"ratio": args.ratio, "res": args.res, "duration": args.duration, "seed": args.seed,
                 "image": args.image, "video": args.video, "audio": args.audio}
    params = build_params(target, prompt, pairs, shortcuts)
    _check(target, params, args.no_validate)
    est_usd, est_note = costs.estimate(target, params)
    if args.dry_run:
        print(json.dumps({"endpoint": target.endpoint, "label": target.label, "params": params,
                          "coste_estimado_usd": est_usd, "coste_nota": est_note}, ensure_ascii=False, indent=2))
        print("Dry-run: validado, no se ha enviado nada.", file=sys.stderr)
        return EXIT_OK
    if args.name:
        target.label = re.sub(r"[^A-Za-z0-9._-]+", "-", args.name)
    if not args.json:
        print(f"Coste estimado: {costs.describe(est_usd, est_note)}", file=sys.stderr)
    return _run_single(target, params, settings, _options(args), args.json)


def _load_batch(path: Path) -> list[dict]:
    text = path.read_text("utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise UsageError("El .json de batch debe ser una lista de trabajos.")
        return data
    items = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            try:
                items.append(json.loads(line))
            except ValueError as exc:
                raise UsageError(f"{path}:{n}: JSON no válido ({exc})") from None
    return items


def cmd_batch(args, settings: Settings) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise UsageError(f"No existe {path}")
    prepared: list[tuple[Target, dict]] = []
    for i, item in enumerate(_load_batch(path), 1):
        if not isinstance(item, dict) or not item.get("model"):
            raise UsageError(f"Trabajo {i}: falta 'model'.")
        target = resolve(item["model"])
        params = {**target.defaults, **(item.get("params") or {})}
        if item.get("prompt") is not None:
            params["prompt"] = item["prompt"]
        if item.get("name"):
            target.label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(item["name"]))
        _check(target, params, args.no_validate, where=f"Trabajo {i}: ")
        prepared.append((target, params))
    if not prepared:
        raise UsageError("El archivo no contiene trabajos.")
    if args.dry_run:
        print(json.dumps([{"endpoint": t.endpoint, "label": t.label, "params": p} for t, p in prepared],
                         ensure_ascii=False, indent=2))
        print(f"Dry-run: {len(prepared)} trabajos validados, no se ha enviado nada.", file=sys.stderr)
        return EXIT_OK

    opts = _options(args)
    cache: dict[Path, str] = {}
    try:  # upload every local file once, before spending anything on generations
        prepared = [(t, jobs.upload_refs(p, opts, cache)) for t, p in prepared]
    except (higgsfield_client.HiggsfieldClientError, httpx.HTTPError, OSError) as exc:
        print(redact(f"Fallo subiendo archivos; no se ha lanzado ningún trabajo: {type(exc).__name__}: {exc}"),
              file=sys.stderr)
        return EXIT_FAILED
    total, stop = len(prepared), threading.Event()
    print(f"Lanzando {total} trabajos con concurrencia {args.concurrency} ...", file=sys.stderr)

    def work(i: int, target: Target, params: dict) -> jobs.JobResult:
        try:
            return jobs.run(target, params, settings, opts, f"[{i}/{total}] ", cache, stop)
        except jobs.JobInterrupted as exc:
            return exc.result
        except Exception as exc:  # a crash in one job must not hide the others' results
            return jobs.JobResult(ok=False, status="error", label=target.label, endpoint=target.endpoint,
                                  params=params, error=redact(f"{type(exc).__name__}: {exc}"), exit_code=EXIT_FAILED)

    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    futures = [pool.submit(work, i, t, p) for i, (t, p) in enumerate(prepared, 1)]
    interrupted = False
    try:
        pending = set(futures)
        while pending:  # short waits keep the main thread responsive to Ctrl+C
            _, pending = futures_wait(pending, timeout=0.5)
    except KeyboardInterrupt:
        interrupted = True
        stop.set()  # queued jobs are skipped; running ones stop polling at their next check
        print("\nInterrumpiendo: no se envían más trabajos ...", file=sys.stderr)
    pool.shutdown(wait=True, cancel_futures=True)
    results = [f.result() for f in futures if f.done() and not f.cancelled()]
    for res in results:
        _report(res, args.json)
    failed = sum(not r.ok for r in results) + (total - len(results))
    warned = sum(r.ok and bool(r.warnings) for r in results)
    print(f"Resumen: {total - failed} OK, {failed} no completados, {warned} con avisos de almacenamiento.",
          file=sys.stderr)
    pending = [r.request_id for r in results if r.request_id and r.status in ("interrupted", "timeout", "lost")]
    if pending:
        print("Siguen en Higgsfield (recupera con hf result <id>): " + " ".join(pending), file=sys.stderr)
    if interrupted or failed:
        return EXIT_FAILED
    return EXIT_STORAGE if warned else EXIT_OK


def cmd_models(args, settings: Settings) -> int:
    cat = load()
    if args.name:
        target = resolve(args.name)
        if target.schema is None:
            print(f"'{target.endpoint}' no está en el catálogo (se puede usar igualmente, sin validación).")
            return EXIT_OK
        schema = target.schema
        print(f"{target.label} → {target.endpoint}\n{target.title} · salida: {target.output}")
        if target.note:
            print(f"Nota: {target.note}")
        print(f"Precio (docs públicas, antes de descuentos): "
              f"{target.pricing if has_public_price(target) else 'no publicado para este modelo'}")
        if target.defaults:
            print(f"Valores fijados por el alias: {json.dumps(target.defaults, ensure_ascii=False)}")
        print()
        required = set(schema.get("required") or [])
        for name, prop in schema.get("properties", {}).items():
            print("  " + describe_param(name, prop, name in required))
        if schema.get("allOf") or schema.get("if"):
            rules = json.dumps(schema.get("allOf") or {"if": schema["if"], "then": schema.get("then")}, ensure_ascii=False)
            print(f"\nReglas condicionales: {rules}")
        if schema.get("additionalProperties") is False:
            print("\nNo admite parámetros fuera de esta lista.")
        return EXIT_OK

    reverse = {v: k for k, v in cat["aliases"].items()}
    query = (args.search or "").lower()
    for kind in ("video", "image"):
        if args.kind and args.kind != kind:
            continue
        print(f"\n{kind.upper()}")
        for endpoint, info in cat["endpoints"].items():
            alias = reverse.get(endpoint, "")
            if info["output"] != kind or (query and query not in f"{alias} {endpoint} {info['title']}".lower()):
                continue
            print(f"  {alias:<22} {endpoint:<50} {info['title']}")
    if not args.kind or args.kind == "image":
        print("\nWORKFLOWS (alias con valores fijados)")
        for name, preset in cat["presets"].items():
            print(f"  {name:<22} {preset['endpoint']:<50} {preset.get('note', '')[:70]}")
    print(f"\nCatálogo: {cat['_meta']['endpoints']} endpoints, esquemas del {cat['_meta']['retrieved']}. "
          f"Detalle: hf models <alias>", file=sys.stderr)
    return EXIT_OK


def _history_lookup(settings: Settings, request_id: str) -> dict:
    for rec in storage.read_history(settings.history_file):
        if rec.get("request_id") == request_id:
            return rec
    return {}


def cmd_result(args, settings: Settings) -> int:
    status_url = api.status_url_for(args.request_id)
    known = _history_lookup(settings, args.request_id)
    if args.model:
        target = resolve(args.model)
    else:
        target = Target(known.get("endpoint", "desconocido"), known.get("label", "result"), None)
    res = jobs.JobResult(ok=False, status="error", label=target.label, endpoint=target.endpoint,
                         request_id=args.request_id, params=known.get("params", {}))
    try:
        res = jobs.finish(res, status_url, settings, _options(args), time.monotonic())
    except jobs.JobInterrupted as exc:
        res = exc.result
    _report(res, args.json)
    return res.exit_code


def cmd_status(args, settings: Settings) -> int:
    print(api.status_of(args.request_id))
    return EXIT_OK


def cmd_cancel(args, settings: Settings) -> int:
    try:
        api.cancel(args.request_id)
    except higgsfield_client.HiggsfieldClientError as exc:
        code = getattr(getattr(exc.__cause__, "response", None), "status_code", None)
        why = "ya empezó o terminó; solo se cancela en cola" if code == 400 else f"HTTP {code}"
        print(redact(f"No se pudo cancelar ({why}): {exc}"))
        return EXIT_FAILED
    print(f"Cancelación aceptada para {args.request_id}")
    return EXIT_OK


def cmd_upload(args, settings: Settings) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise UsageError(f"No existe {path}")
    print(api.upload_file(path))
    return EXIT_OK


def cmd_history(args, settings: Settings) -> int:
    records = storage.read_history(settings.history_file)[-args.n:]
    if args.json:
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return EXIT_OK
    if not records:
        print(f"Sin historial en {settings.history_file}")
    for rec in records:
        first = (rec.get("outputs") or [{}])[0]
        where = first.get("file") or first.get("url") or rec.get("error") or ""
        print(f"{rec.get('ts', '')[:19]}  {rec.get('status', ''):<11} {rec.get('label', ''):<20} "
              f"{rec.get('request_id', '')}  {where}")
    return EXIT_OK


def cmd_spend(args, settings: Settings) -> int:
    report = costs.spend_report(storage.read_history(settings.history_file), args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"Gasto de los últimos {args.days:g} días (desde {report['desde']})\n")
    print(f"{'modelo':<22} {'lanzadas':>8} {'hechas':>7} {'no cobradas':>12} {'pend/incierto':>14} {'USD est.':>9} {'sin precio':>11}")
    rows = sorted(report["por_modelo"].items(), key=lambda kv: -kv[1]["usd_estimado"])
    for name, r in rows + [("TOTAL", report["total"])]:
        print(f"{name:<22} {r['lanzadas']:>8} {r['completadas']:>7} {r['no_cobradas']:>12} "
              f"{r['pendientes_o_inciertas']:>14} {r['usd_estimado']:>9.2f} {r['completadas_sin_precio']:>11}")
    print(f"\n{report['nota']}")
    return EXIT_OK


def cmd_presets(args, settings: Settings) -> int:
    resp = api.raw_get("/marketing-studio/image/presets", {"size": args.size})
    if resp.status_code != 200:
        print(f"La API respondió HTTP {resp.status_code}: {resp.text[:300]}")
        return EXIT_FAILED
    data = resp.json()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    items = data if isinstance(data, list) else next(
        (data[k] for k in ("items", "presets", "data", "results") if isinstance(data.get(k), list)), None)
    if items is None:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    for item in items:
        name = item.get("name") or item.get("title") or ""
        print(f"{item.get('id', ''):<38} {name}")
    return EXIT_OK


ENV_TEMPLATE = """# Configuración local de hf. No lo compartas ni lo subas a Git.
# Higgsfield API, formato key-id:key-secret (https://open.higgsfield.ai/api-keys)
HF_KEY=

# Opcional: copia persistente en Cloudinary. Formato cloudinary://API_KEY:API_SECRET@CLOUD_NAME
CLOUDINARY_URL=
"""


def cmd_init(args, settings: Settings) -> int:
    """Prepare a new machine: create the config file (never overwrites) and say where things live."""
    env_file = settings.home / ".env.local"
    settings.home.mkdir(parents=True, exist_ok=True)
    if env_file.exists():
        print(f"Ya existe {env_file}; no se modifica.")
    else:
        env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        try:
            os.chmod(env_file, 0o600)  # only the owner can read the credentials
        except OSError:
            pass
        print(f"Creado {env_file}")
    print(f"Salidas e historial: {settings.output_dir}")
    if args.open:
        opener = (["open", "-t"] if sys.platform == "darwin" else
                  ["notepad"] if sys.platform.startswith("win") else [os.getenv("EDITOR") or "xdg-open"])
        try:
            subprocess.run([*opener, str(env_file)], check=False)
        except OSError as exc:
            print(f"No pude abrir el editor ({exc}); ábrelo a mano.")
    print("Siguiente paso: rellena HF_KEY (y CLOUDINARY_URL si la quieres), guarda y ejecuta `hf doctor`.")
    return EXIT_OK


def claude_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def mcp_entry() -> dict:
    # Absolute interpreter of this install: works from the desktop app without any PATH setup.
    return {"command": sys.executable, "args": ["-m", "hfcli", "mcp"]}


def mcp_available() -> bool:
    import importlib.util
    return sys.version_info >= (3, 10) and importlib.util.find_spec("mcp") is not None


def install_mcp(config_path: Path) -> str:
    """Add (or update) the 'higgsfield' server in Claude's config without touching anything else.
    Keeps the file's permissions, follows symlinks, and never overwrites an earlier backup."""
    if not mcp_available():
        raise UsageError("Falta el soporte MCP en esta instalación (Python 3.10+ y pip install -e '.[mcp]'). "
                         "No toco la configuración de Claude.")
    config_path = config_path.expanduser()
    target = config_path.resolve() if config_path.is_symlink() else config_path
    data: dict = {}
    mode = None
    if target.exists():
        original = target.read_bytes()
        try:
            data = json.loads(original.decode("utf-8") or "{}")
        except ValueError as exc:
            raise UsageError(f"{target} no es JSON válido ({exc}); no lo toco. Arréglalo y repite.") from None
        if not isinstance(data, dict):
            raise UsageError(f"{target} no tiene el formato esperado; no lo toco.")
        mode = target.stat().st_mode & 0o777
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1000):  # never overwrite an existing backup
            backup = target.with_name(f"{target.name}.bak-{stamp}" + (f"-{n}" if n else ""))
            try:
                with backup.open("xb") as fh:
                    fh.write(original)
                break
            except FileExistsError:
                continue
        os.chmod(backup, mode)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise UsageError(f"'mcpServers' en {target} no es un objeto; no lo toco.")
    previous = servers.get("higgsfield")
    servers["higgsfield"] = mcp_entry()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(target)
    return "actualizado" if previous else "añadido"


def cmd_mcp(args, settings: Settings) -> int:
    if args.action == "print":
        print(json.dumps({"mcpServers": {"higgsfield": mcp_entry()}}, indent=2))
        return EXIT_OK
    if args.action == "install":
        path = Path(args.config).expanduser() if args.config else claude_config_path()
        what = install_mcp(path)
        print(f"Servidor 'higgsfield' {what} en {path} (copia de seguridad junto al archivo).")
        print("Cierra la app de Claude del todo (Cmd+Q) y vuelve a abrirla para cargarlo.")
        return EXIT_OK
    try:
        from hfcli.mcp_server import serve
    except ImportError as exc:
        raise UsageError(f"Falta el soporte MCP ({exc.name}). Instala: pip install -e '.[mcp]' (Python 3.10+).") from None
    serve()
    return EXIT_OK


def doctor_report(settings: Settings) -> tuple[list[str], int]:
    """Checks that cost nothing. Returns (lines, number of FAIL lines); never includes secrets."""
    lines: list[str] = []
    fails = 0

    def line(state: str, text: str) -> None:
        nonlocal fails
        fails += state == "FAIL"
        lines.append(f"[{state:<4}] {text}")

    try:
        sdk = version("higgsfield-client")
    except PackageNotFoundError:
        sdk = "?"
    line("OK", f"hf {__version__} · higgsfield-client {sdk} · Python {sys.version.split()[0]}")
    env_file = settings.home / ".env.local"
    line("OK" if env_file.exists() else "WARN", f"Config: {env_file} {'(existe)' if env_file.exists() else '(no existe)'}")
    usable, detail = credential_state()
    line("OK" if usable else "FAIL", f"Credenciales: {detail} (valor no mostrado)")
    if usable:
        # Non-billable probe: status of a request id that cannot exist. 401 = key rejected.
        try:
            code = api.raw_get("/requests/00000000-0000-0000-0000-000000000000/status").status_code
            if code == 401:
                line("FAIL", "La API rechaza la clave (HTTP 401)")
            elif code == 403:
                line("WARN", "La API devuelve 403: clave reconocida pero sin permiso o sin saldo")
            else:
                line("OK", f"La API acepta la clave (sonda sin coste, HTTP {code})")
        except httpx.HTTPError as exc:
            line("FAIL", f"No hay conexión con api.higgsfield.ai: {type(exc).__name__}")
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.output_dir):
            pass
        line("OK", f"Salida: {settings.output_dir} (escribible)")
    except OSError as exc:
        line("FAIL", f"Salida: {settings.output_dir} no escribible ({exc})")
    if settings.cloudinary_configured:
        try:
            import cloudinary
            import cloudinary.api
            cloudinary.config(secure=True)
            cloudinary.api.ping()
            line("OK", f"Cloudinary: conectado (carpeta {settings.cloudinary_folder}/)")
        except ImportError:
            line("FAIL", "Cloudinary: CLOUDINARY_URL definida pero falta el paquete: pip install -e '.[cloudinary]'")
        except Exception as exc:
            line("FAIL", redact(f"Cloudinary: {type(exc).__name__}: {exc}"))
    else:
        line("INFO", "Cloudinary: desactivado (sin CLOUDINARY_URL)")
    meta = load()["_meta"]
    line("OK", f"Catálogo: {meta['endpoints']} endpoints (esquemas del {meta['retrieved']})")
    return lines, fails


def cmd_doctor(args, settings: Settings) -> int:
    lines, fails = doctor_report(settings)
    print("\n".join(lines))
    return EXIT_USAGE if fails else EXIT_OK


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hf", description="Higgsfield API desde la terminal.")
    parser.add_argument("--version", action="version", version=f"hf {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gen", help="generar imagen o vídeo", description=(
        "hf gen <modelo> [\"prompt\"] [clave=valor ...]. Archivos locales como @ruta. "
        "Ej: hf gen seedance-2.5-i2v \"slow push-in\" --image @frame.png --duration 8"))
    p.add_argument("model", help="alias (hf models), workflow o endpoint completo")
    p.add_argument("inputs", nargs="*", help="prompt y/o parámetros clave=valor")
    p.add_argument("--prompt", help="prompt explícito (si contiene '=')")
    p.add_argument("--ratio", help="aspect_ratio, p. ej. 16:9")
    p.add_argument("--res", help="resolution, p. ej. 720p")
    p.add_argument("--duration", help="duración en segundos")
    p.add_argument("--seed", help="semilla")
    p.add_argument("--image", action="append", help="imagen: URL o @archivo (repetible)")
    p.add_argument("--video", action="append", help="vídeo: URL o @archivo (repetible)")
    p.add_argument("--audio", action="append", help="audio: URL o @archivo (repetible)")
    p.add_argument("--name", help="etiqueta para archivos e historial")
    p.add_argument("--dry-run", action="store_true", help="validar y mostrar la petición sin enviarla")
    p.add_argument("--no-validate", action="store_true", help="no validar contra el esquema del catálogo")
    _add_storage_flags(p)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("batch", help="lanzar varios trabajos desde .jsonl/.json")
    p.add_argument("file")
    p.add_argument("--concurrency", type=_int_bounded(1, 8), default=2, help="trabajos simultáneos (1-8, def. 2)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--quiet", action="store_true", help="sin progreso por trabajo")
    _add_storage_flags(p)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("models", help="listar modelos o ver los parámetros de uno")
    p.add_argument("name", nargs="?")
    p.add_argument("--kind", choices=["video", "image"])
    p.add_argument("--search", help="filtrar por texto")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("result", help="esperar/recuperar una petición existente y guardar sus archivos")
    p.add_argument("request_id")
    p.add_argument("--model", help="alias para nombrar archivos si no está en el historial")
    _add_storage_flags(p)
    p.set_defaults(func=cmd_result)

    for name, func, text in (("status", cmd_status, "estado de una petición"),
                             ("cancel", cmd_cancel, "cancelar una petición en cola")):
        p = sub.add_parser(name, help=text)
        p.add_argument("request_id")
        p.set_defaults(func=func)

    p = sub.add_parser("upload", help="subir un archivo local y obtener su URL")
    p.add_argument("file")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("history", help="últimas generaciones")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("spend", help="cuánto se ha lanzado y gastado (estimado) por modelo")
    p.add_argument("--days", type=_bounded(0.01, 3650), default=30, help="ventana en días (def. 30)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spend)

    p = sub.add_parser("presets", help="presets de Marketing Studio (para preset_id)")
    p.add_argument("--size", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("mcp", help="servidor MCP para que Claude use hf en este ordenador")
    p.add_argument("action", nargs="?", choices=["serve", "install", "print"], default="serve",
                   help="serve (por defecto, lo lanza la app de Claude), install (lo registra en Claude), print")
    p.add_argument("--config", help="ruta alternativa de claude_desktop_config.json (install)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("init", help="preparar este ordenador: crea el archivo de configuración")
    p.add_argument("--open", action="store_true", help="abrirlo en el editor al terminar")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="comprobar configuración sin gastar créditos")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # parse_known_args lets key=value pairs appear after options (argparse won't intermix them otherwise)
    args, extra = parser.parse_known_args(argv)
    unknown = [x for x in extra if x.startswith("-") or not hasattr(args, "inputs")]
    if unknown:
        parser.error(f"argumentos no reconocidos: {' '.join(unknown)}")
    if extra:
        args.inputs = list(args.inputs) + extra
    settings = load_settings()
    try:
        return args.func(args, settings)
    except UsageError as exc:
        print(redact(f"Error: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    except higgsfield_client.HiggsfieldClientError as exc:
        print(redact(f"Error de la API: {exc}"), file=sys.stderr)
        return EXIT_FAILED
    except (httpx.HTTPError, ValueError) as exc:
        print(redact(f"Error de red o respuesta inesperada: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("Interrumpido.", file=sys.stderr)
        return EXIT_FAILED
    except BrokenPipeError:  # output piped into `head` and closed early
        sys.stdout = open(os.devnull, "w")
        return EXIT_OK if args.command in {"models", "history", "presets", "doctor", "status", "spend"} else EXIT_FAILED
