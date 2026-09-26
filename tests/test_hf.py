"""Offline tests. The API is faked at hfcli.api (submit/get_json) or with httpx.MockTransport;
downloads hit a local HTTP server. No credentials, no network to Higgsfield, no cost."""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hfcli import EXIT_FAILED, EXIT_OK, EXIT_STORAGE, EXIT_USAGE, api, cli, storage
from hfcli.catalog import resolve, validate
from hfcli.params import build_params, split_inputs

RID = "f4017ce6-b83d-4d79-b7ec-46beb4b54a7d"
REAL_SLEEP = time.sleep  # captured before fixtures patch time.sleep
STATUS_URL = f"https://platform.higgsfield.ai/requests/{RID}/status"


# ---------- fixtures & fakes ----------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HFCLI_HOME", str(tmp_path))
    monkeypatch.setenv("HF_KEY", "test-id:test-secret-value")
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    monkeypatch.setattr(api, "_http_client", None)
    monkeypatch.setattr(api, "_sdk_client", None)
    return tmp_path


@pytest.fixture
def media_server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000)
    (root / "img1.png").write_bytes(b"\x89PNG" + b"y" * 500)
    (root / "img2.png").write_bytes(b"\x89PNG" + b"z" * 500)
    (root / "noext").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    class Handler(SimpleHTTPRequestHandler):
        def guess_type(self, path):
            return "video/mp4" if path.endswith("noext") else super().guess_type(path)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fake_api(monkeypatch, statuses, final=None, seen=None, request_id=RID):
    """submit() returns a request; get_json() walks through `statuses`, the last one carrying `final`."""
    seq = [{"status": s} for s in statuses[:-1]] + [{"status": statuses[-1], "request_id": request_id, **(final or {})}]

    def submit(endpoint, params):
        if seen is not None:
            seen.append((endpoint, params))
        return {"request_id": request_id, "status": "queued", "status_url": STATUS_URL}

    def get_json(url, timeout=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(api, "submit", submit)
    monkeypatch.setattr(api, "get_json", get_json)


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def mock_http(monkeypatch, handler):
    """Route the real api.submit/get_json through the SDK transport into an httpx.MockTransport."""
    client = httpx.Client(base_url=api.API_BASE, transport=httpx.MockTransport(handler),
                          headers={"Authorization": "Key test-id:test-secret-value"})
    monkeypatch.setattr(api, "http", lambda: client)


# ---------- params & catalog ----------

def test_split_inputs_and_coercion():
    target = resolve("seedance-2.5")
    prompt, pairs = split_inputs(["a sunset", "duration=8", "generate_audio=false"], target.properties)
    params = build_params(target, prompt, pairs, {"ratio": "9:16"})
    assert params == {"prompt": "a sunset", "duration": 8, "generate_audio": False, "aspect_ratio": "9:16"}
    assert validate(target, params) == []


def test_prompt_with_equals_is_not_a_param():
    target = resolve("kling-3")
    prompt, pairs = split_inputs(["Shot=wide, a cat on a roof"], target.properties)
    assert prompt == "Shot=wide, a cat on a roof" and pairs == []


def test_repeated_array_key_appends_and_preset_defaults():
    target = resolve("product-shots")
    params = build_params(target, "tube", ["image_urls=https://a/1.png", "image_urls=https://a/2.png"], {})
    assert params["image_urls"] == ["https://a/1.png", "https://a/2.png"]
    assert params["enhance_prompt"] is True and params["preset_id"].startswith("05d695d1")
    assert validate(target, params) == []


def test_all_catalog_schemas_compile():
    from jsonschema import Draft202012Validator
    from hfcli.catalog import load
    cat = load()
    for info in cat["endpoints"].values():
        Draft202012Validator.check_schema(info["schema"])
    assert set(cat["aliases"].values()) <= set(cat["endpoints"])
    assert len(cat["endpoints"]) == 52


def test_extract_outputs_shapes():
    result = {"status": "completed", "request_id": "r", "status_url": "https://x/s", "cancel_url": "https://x/c",
              "images": [{"url": "https://cdn/a.png"}, {"url": "https://cdn/b.png"}],
              "video": {"url": "https://cdn/v.mp4", "content_type": "video/mp4"}, "zip_url": "https://cdn/p.zip"}
    urls = [o["url"] for o in storage.extract_outputs(result)]
    assert urls == ["https://cdn/a.png", "https://cdn/b.png", "https://cdn/v.mp4", "https://cdn/p.zip"]


@pytest.mark.parametrize("argv,needle", [
    (["gen", "kling-3", "a cat", "durtion=10"], "¿Querías 'duration'?"),
    (["gen", "z-image", "apple", "style=anime"], "Parámetro desconocido 'style'"),
    (["gen", "seedance-2.5-i2v", "x", "--image", "frames/intro.png"], "te falta la @"),
    (["gen", "z-image", "@README.md"], "@archivo solo vale en campos de URL"),
    (["gen", "https://evil.example/x", "hi"], "Nombre de modelo no válido"),
    (["gen", "seedance-2.5", "x", "--ratio", "5:4"], "aspect_ratio"),
    (["gen", "ltx-fast", "a long enough prompt", "--duration", "7"], "7 is not one of [6, 8, 10]"),
    (["gen", "product-shots", "cream tube"], "'image_urls' is a required property"),
])
def test_invalid_input_is_rejected_before_spending(home, monkeypatch, capsys, argv, needle):
    monkeypatch.setattr(api, "submit", lambda *a: pytest.fail("must not submit"))
    monkeypatch.chdir(cli.Path(__file__).resolve().parents[1])
    code, _, err = run_cli(capsys, *argv)
    assert code == EXIT_USAGE and needle in err


@pytest.mark.parametrize("flag,value", [("--poll", "-1"), ("--timeout", "0"), ("--poll", "0.1")])
def test_bad_numeric_flags_rejected(home, flag, value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["gen", "z-image", "apple", flag, value])
    assert exc.value.code == 2


# ---------- gen end to end ----------

def test_gen_completed_downloads_and_logs(home, media_server, monkeypatch, capsys):
    seen = []
    fake_api(monkeypatch, ["queued", "in_progress", "completed"], {"video": {"url": f"{media_server}/clip.mp4"}}, seen)
    code, out, err = run_cli(capsys, "gen", "seedance-2.5", "A cinematic scene at sunset", "--duration", "5",
                             "--res", "720p", "--ratio", "16:9", "--json")
    assert code == EXIT_OK, err
    res = json.loads(out)
    assert res["ok"] and res["status"] == "completed" and res["request_id"] == RID
    assert seen == [("bytedance/seedance-2.5/text-to-video",
                     {"prompt": "A cinematic scene at sunset", "duration": 5, "resolution": "720p", "aspect_ratio": "16:9"})]
    file = res["outputs"][0]["file"]
    assert file.endswith(f"seedance-2.5_{RID}.mp4") and open(file, "rb").read().startswith(b"\x00\x00\x00\x18ftyp")
    history = storage.read_history(home / "output" / "history.jsonl")
    assert history[-1]["status"] == "completed" and history[-1]["outputs"][0]["file"] == file
    assert "estado: in_progress" in err and "Coste orientativo" not in err  # --json keeps stderr lean


@pytest.mark.parametrize("final_status,final,needle", [
    ("failed", {"error": "boom"}, "falló: boom"),
    ("nsfw", {}, "moderación"),
    ("canceled", {}, "cancelada"),
    ("cancelled", {}, "cancelada"),
    ("completed", {}, "sin URL"),
])
def test_gen_non_success_is_failure(home, monkeypatch, capsys, final_status, final, needle):
    fake_api(monkeypatch, ["queued", final_status], final)
    code, out, _ = run_cli(capsys, "gen", "z-image", "a red apple")
    assert code == EXIT_FAILED and out.startswith("FALLO") and needle in out
    assert storage.read_history(home / "output" / "history.jsonl")[-1]["ok"] is False


def test_unknown_status_keeps_polling(home, media_server, monkeypatch, capsys):
    fake_api(monkeypatch, ["queued", "expired_but_retrying", "completed"], {"images": [{"url": f"{media_server}/img1.png"}]})
    code, _, err = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_OK and "desconocido, sigo esperando" in err


def test_gen_timeout_keeps_request_id(home, monkeypatch, capsys):
    fake_api(monkeypatch, ["queued"])
    clock = iter(range(0, 100_000, 100))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple", "--timeout", "1")
    assert code == EXIT_FAILED and "[timeout]" in out and f"hf result {RID}" in out


def test_errors_after_submit_never_escape(home, monkeypatch, capsys):
    fake_api(monkeypatch, ["queued"])
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: (_ for _ in ()).throw(httpx.ConnectError("down")))
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple", "--json")
    res = json.loads(out)
    assert code == EXIT_FAILED and res["status"] == "lost" and res["request_id"] == RID
    assert storage.read_history(home / "output" / "history.jsonl")[-1]["status"] == "lost"


def test_gen_uploads_local_file_before_submit(home, media_server, tmp_path, monkeypatch, capsys):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG")
    uploads, seen = [], []
    monkeypatch.setattr(api, "upload_file", lambda p: uploads.append(p) or "https://cdn/uploaded.png")
    fake_api(monkeypatch, ["completed"], {"video": {"url": f"{media_server}/clip.mp4"}}, seen)
    code, _, err = run_cli(capsys, "gen", "seedance-2.5-i2v", "slow push-in", "--image", f"@{frame}", "--no-download")
    assert code == EXIT_OK, err
    assert uploads == [frame] and seen[0][1]["image_url"] == "https://cdn/uploaded.png"


def test_download_failure_is_storage_warning(home, media_server, monkeypatch, capsys):
    fake_api(monkeypatch, ["completed"], {"images": [{"url": f"{media_server}/missing.png"}]})
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_STORAGE and "AVISO: descarga fallida" in out
    assert not list((home / "output").rglob("*.part"))


def test_extension_from_content_type(home, media_server, monkeypatch, capsys):
    fake_api(monkeypatch, ["completed"], {"video": {"url": f"{media_server}/noext"}})
    code, out, _ = run_cli(capsys, "gen", "seedance-2.5", "x")
    assert code == EXIT_OK and f"seedance-2.5_{RID}.mp4" in out


def test_cloudinary_upload_called_with_local_file(home, media_server, monkeypatch, capsys):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://k:s@demo")
    calls = []
    monkeypatch.setattr(storage, "cloudinary_upload", lambda src, **kw: calls.append((src, kw)) or "https://res.cloudinary.com/demo/x.png")
    fake_api(monkeypatch, ["completed"], {"images": [{"url": f"{media_server}/img1.png"}, {"url": f"{media_server}/img2.png"}]})
    code, out, _ = run_cli(capsys, "gen", "soul-2", "portrait", "--json")
    res = json.loads(out)
    assert code == EXIT_OK and len(calls) == 2
    assert calls[0][0].endswith(f"soul-2_{RID}_1.png") and calls[0][1]["folder"].startswith("higgsfield/")
    assert all(o["cloudinary_url"].startswith("https://res.cloudinary.com") for o in res["outputs"])


# ---------- submit safety (real api.submit through the SDK transport) ----------

def test_submit_not_retried_on_5xx_and_reported_uncertain(home, monkeypatch, capsys):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(502, json={"detail": "bad gateway"})
    mock_http(monkeypatch, handler)
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert len(posts) == 1, "a 5xx on the generation POST must not be retried (could bill twice)"
    assert code == EXIT_FAILED and "[uncertain]" in out and "no se sabe si la petición llegó" in out
    hist = (home / "output" / "history.jsonl").read_text()
    assert "submit_uncertain" in hist


def test_submit_retried_on_429_only(home, monkeypatch, capsys, media_server):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(429, json={"detail": "slow down"})
            return httpx.Response(200, json={"request_id": RID, "status": "queued", "status_url": STATUS_URL})
        return httpx.Response(200, json={"status": "completed", "request_id": RID,
                                         "images": [{"url": f"{media_server}/img1.png"}]})
    mock_http(monkeypatch, handler)
    code, _, err = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_OK, err
    assert len(posts) == 2


def test_read_timeout_on_submit_is_uncertain(home, monkeypatch, capsys):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    mock_http(monkeypatch, handler)
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_FAILED and "[uncertain]" in out


def test_connect_error_on_submit_is_safe_failure(home, monkeypatch, capsys):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)
    mock_http(monkeypatch, handler)
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_FAILED and "no se ha enviado nada" in out


def test_4xx_on_submit_is_clean_rejection(home, monkeypatch, capsys):
    mock_http(monkeypatch, lambda r: httpx.Response(403, json={"detail": "Not enough credits"}))
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_FAILED and "HTTP 403" in out and "Not enough credits" in out


def test_foreign_status_url_is_ignored(home, monkeypatch, capsys, media_server):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": RID, "status_url": "https://evil.example/steal"})
        return httpx.Response(200, json={"status": "completed", "images": [{"url": f"{media_server}/img1.png"}]})
    mock_http(monkeypatch, handler)
    code, _, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_OK and "evil.example" not in hosts


# ---------- secrets ----------

def test_key_with_whitespace_is_stripped_and_never_printed(home, monkeypatch, capsys):
    monkeypatch.setenv("HF_KEY", "test-id:test-secret-value \n")
    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers["authorization"])
        return httpx.Response(401, json={"detail": "Invalid credentials"})
    client = httpx.Client(base_url=api.API_BASE, transport=httpx.MockTransport(handler),
                          headers={"Authorization": f"Key {api._key()}"})
    monkeypatch.setattr(api, "http", lambda: client)
    code, out, err = run_cli(capsys, "gen", "z-image", "apple", "--json")
    assert seen_headers == ["Key test-id:test-secret-value"]
    assert "test-secret-value" not in out + err
    assert "test-secret-value" not in (home / "output").joinpath("history.jsonl").read_text() if (home / "output" / "history.jsonl").exists() else True


def test_redact_in_error_messages(home, monkeypatch, capsys):
    fake_api(monkeypatch, ["failed"], {"error": "upstream echoed Key test-id:test-secret-value"})
    code, out, _ = run_cli(capsys, "gen", "z-image", "apple")
    assert code == EXIT_FAILED and "test-secret-value" not in out and "***" in out
    assert "test-secret-value" not in (home / "output" / "history.jsonl").read_text()


def test_doctor_does_not_print_secret(home, monkeypatch, capsys):
    class R:
        status_code = 404
    monkeypatch.setattr(api, "raw_get", lambda *a, **k: R())
    code, out, _ = run_cli(capsys, "doctor")
    assert code == EXIT_OK and "test-secret-value" not in out and "acepta la clave" in out


# ---------- batch ----------

def _batch_file(tmp_path, *items):
    path = tmp_path / "jobs.jsonl"
    path.write_text("\n".join(json.dumps(i) if isinstance(i, dict) else i for i in items))
    return path


def test_batch_validates_all_first_then_runs(home, media_server, tmp_path, monkeypatch, capsys):
    jobs_file = _batch_file(tmp_path, {"model": "z-image", "prompt": "apple", "name": "apple"}, "# comment",
                            {"model": "seedance-2.5", "prompt": "sunset", "params": {"duration": 6}})
    seen, lock = [], threading.Lock()

    def submit(endpoint, params):
        with lock:
            seen.append(endpoint)
            n = len(seen)
        return {"request_id": f"{n:08d}-aaaa-bbbb", "status_url": f"https://platform.higgsfield.ai/requests/{endpoint}"}

    def get_json(url, timeout=None):
        if "seedance" in url:
            return {"status": "completed", "video": {"url": f"{media_server}/clip.mp4"}}
        return {"status": "completed", "images": [{"url": f"{media_server}/img1.png"}]}
    monkeypatch.setattr(api, "submit", submit)
    monkeypatch.setattr(api, "get_json", get_json)
    code, out, err = run_cli(capsys, "batch", str(jobs_file), "--concurrency", "2")
    assert code == EXIT_OK, err
    assert sorted(seen) == ["bytedance/seedance-2.5/text-to-video", "z-image/turbo"]
    assert out.count("OK ") == 2 and "2 OK, 0 no completados" in err


def test_batch_invalid_job_aborts_before_spending(home, tmp_path, monkeypatch, capsys):
    jobs_file = _batch_file(tmp_path, {"model": "z-image", "prompt": "ok"},
                            {"model": "seedance-2.5", "prompt": "x", "params": {"durtion": 6}})
    monkeypatch.setattr(api, "submit", lambda *a: pytest.fail("must not submit"))
    code, _, err = run_cli(capsys, "batch", str(jobs_file))
    assert code == EXIT_USAGE and "Trabajo 2" in err and "durtion" in err


def test_batch_crash_in_one_job_keeps_other_results(home, media_server, tmp_path, monkeypatch, capsys):
    jobs_file = _batch_file(tmp_path, {"model": "z-image", "prompt": "a"}, {"model": "z-image", "prompt": "b"})
    calls = []

    def submit(endpoint, params):
        calls.append(params["prompt"])
        if params["prompt"] == "b":
            raise RuntimeError("unexpected")
        return {"request_id": RID, "status_url": STATUS_URL}
    monkeypatch.setattr(api, "submit", submit)
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: {"status": "completed", "images": [{"url": f"{media_server}/img1.png"}]})
    code, out, err = run_cli(capsys, "batch", str(jobs_file), "--concurrency", "1")
    assert code == EXIT_FAILED and "OK z-image" in out and "RuntimeError" in out and "1 OK, 1 no completados" in err


def test_batch_ctrl_c_stops_sending_new_jobs(home, tmp_path, monkeypatch, capsys):
    jobs_file = _batch_file(tmp_path, *[{"model": "z-image", "prompt": f"p{i}"} for i in range(5)])
    submitted = []
    monkeypatch.setattr(api, "submit", lambda e, p: submitted.append(p) or {"request_id": f"{len(submitted):08d}-x",
                                                                           "status_url": STATUS_URL})
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: {"status": "in_progress"})
    monkeypatch.setattr(api.time, "sleep", lambda s: REAL_SLEEP(0.01))
    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    code, out, err = run_cli(capsys, "batch", str(jobs_file), "--concurrency", "1")
    assert code == EXIT_FAILED
    assert len(submitted) == 1, "no new job may be submitted after Ctrl+C"
    assert "[interrupted]" in out and "hf result 00000001-x" in out


# ---------- history & result ----------

def test_history_merges_by_request_id(tmp_path):
    hist = tmp_path / "h.jsonl"
    storage.append_history(hist, {"request_id": "a", "status": "queued", "label": "x"})
    storage.append_history(hist, {"request_id": "b", "status": "queued"})
    storage.append_history(hist, {"request_id": "a", "status": "completed"})
    recs = storage.read_history(hist)
    assert [r["request_id"] for r in recs] == ["b", "a"]
    assert recs[-1]["status"] == "completed" and recs[-1]["label"] == "x"


def test_result_recovers_with_history_label(home, media_server, monkeypatch, capsys):
    storage.append_history(home / "output" / "history.jsonl",
                           {"event": "submitted", "request_id": "abcd1234-zz", "label": "kling-3",
                            "endpoint": "kling-video/v3.0/std/text-to-video"})
    urls = []
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: urls.append(url) or {"status": "completed", "video": {"url": f"{media_server}/clip.mp4"}})
    code, out, _ = run_cli(capsys, "result", "abcd1234-zz")
    assert code == EXIT_OK and "kling-3_abcd1234-zz.mp4" in out
    assert urls[0] == "https://api.higgsfield.ai/requests/abcd1234-zz/status"


def test_result_rejects_weird_request_id(home, capsys):
    code, _, err = run_cli(capsys, "result", "../../x")
    assert code == EXIT_USAGE and "request_id no válido" in err


def test_init_creates_private_config_once(home, capsys):
    env_file = home / ".env.local"
    code, out, _ = run_cli(capsys, "init")
    assert code == EXIT_OK and env_file.exists() and "HF_KEY=" in env_file.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    env_file.write_text("HF_KEY=mine:value\n")
    code, out, _ = run_cli(capsys, "init")
    assert "no se modifica" in out and env_file.read_text() == "HF_KEY=mine:value\n"


# ---------- cost estimates & spend report ----------

def test_estimates():
    from hfcli import costs
    t = resolve("seedance-2.5-i2v")
    assert costs.estimate(t, {"duration": 4, "resolution": "480p", "image_url": "https://x"})[0] == 0.8224
    assert costs.estimate(resolve("wan-3"), {"duration": 10, "resolution": "1080p"})[0] == 2.0
    usd, note = costs.estimate(resolve("minimax-h3-ref"), {"duration": 6, "image_urls": ["https://x"] * 7})
    assert usd == round(0.13 * 6 + 0.08 * 2, 4) and "2 referencias extra" in note
    assert costs.estimate(resolve("seedance-2.5-ref"), {"duration": 5, "video_urls": ["https://v"]})[0] is None
    assert costs.estimate(resolve("z-image"), {"prompt": "a"})[0] is None
    assert costs.estimate(resolve("kling-3"), {"prompt": "a"})[1] == "precio no publicado para este modelo"


def test_spend_report_classifies(tmp_path):
    from datetime import datetime, timedelta
    from hfcli import costs
    now = datetime.now().astimezone()
    old = (now - timedelta(days=40)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    hist = [
        {"ts": recent, "event": "final", "request_id": "a", "label": "seedance-2.5", "status": "completed", "est_usd": 0.82},
        {"ts": recent, "event": "final", "request_id": "b", "label": "seedance-2.5", "status": "failed", "est_usd": 0.82},
        {"ts": recent, "event": "final", "request_id": "c", "label": "z-image", "status": "completed", "est_usd": None},
        {"ts": recent, "event": "final", "request_id": "d", "label": "z-image", "status": "nsfw"},
        {"ts": recent, "event": "submitted", "request_id": "e", "label": "kling-3", "status": "queued"},
        {"ts": recent, "event": "submit_uncertain", "label": "kling-3", "status": "uncertain"},
        {"ts": old, "event": "final", "request_id": "f", "label": "seedance-2.5", "status": "completed", "est_usd": 5.0},
    ]
    r = costs.spend_report(hist, 30, now)
    assert r["total"] == {"lanzadas": 6, "completadas": 2, "no_cobradas": 2, "pendientes_o_inciertas": 2,
                          "completadas_sin_precio": 1, "usd_estimado": 0.82}
    assert r["por_modelo"]["seedance-2.5"]["usd_estimado"] == 0.82


def test_gen_records_estimate_and_spend_cli(home, media_server, monkeypatch, capsys):
    fake_api(monkeypatch, ["completed"], {"video": {"url": f"{media_server}/clip.mp4"}})
    code, _, err = run_cli(capsys, "gen", "seedance-2.5", "sunset", "--duration", "4", "--res", "480p")
    assert code == EXIT_OK and "Coste estimado: ~$0.82" in err
    code, out, _ = run_cli(capsys, "spend", "--days", "1")
    assert code == EXIT_OK and "seedance-2.5" in out and "0.82" in out and "open.higgsfield.ai/usage" in out
