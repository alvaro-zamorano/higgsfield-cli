"""MCP layer: money guards, bounded waits, previews, config install, and a real stdio round-trip."""
from __future__ import annotations

import json
import os
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

pytest.importorskip("mcp")

from hfcli import api, cli, storage  # noqa: E402
from hfcli import mcp_server as m  # noqa: E402
from hfcli.config import UsageError  # noqa: E402
from pathlib import Path  # noqa: E402

RID = "f4017ce6-b83d-4d79-b7ec-46beb4b54a7d"
REAL_SLEEP = time.sleep


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HFCLI_HOME", str(tmp_path))
    monkeypatch.setenv("HF_KEY", "test-id:test-secret-value")
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)
    monkeypatch.delenv("HFCLI_MCP_MAX_JOBS_PER_DAY", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def media_server(tmp_path):
    from PIL import Image as PILImage
    root = tmp_path / "srv"
    root.mkdir()
    PILImage.new("RGB", (1280, 720), (200, 30, 30)).save(root / "img.png")
    (root / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 500)
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fake_submit(monkeypatch, request_ids=None):
    sent = []

    def submit(endpoint, params):
        sent.append((endpoint, params))
        rid = (request_ids or [RID])[min(len(sent), len(request_ids or [RID])) - 1]
        return {"request_id": rid, "status_url": f"https://platform.higgsfield.ai/requests/{rid}/status"}
    monkeypatch.setattr(api, "submit", submit)
    return sent


# ---------- generate ----------

def test_dry_run_is_default_and_sends_nothing(home, monkeypatch):
    monkeypatch.setattr(api, "submit", lambda *a: pytest.fail("dry run must not submit"))
    out = m.generate_impl("seedance-2.5", "a sunset", {"duration": 5, "aspect_ratio": "16:9"})
    assert out["dry_run"] is True and out["endpoint"] == "bytedance/seedance-2.5/text-to-video"
    assert out["params"] == {"duration": 5, "aspect_ratio": "16:9", "prompt": "a sunset"}
    assert "$0.4622 at 720p" in out["pricing"]


def test_invalid_params_raise_before_spending(home, monkeypatch):
    monkeypatch.setattr(api, "submit", lambda *a: pytest.fail("must not submit"))
    with pytest.raises(UsageError, match="durtion"):
        m.generate_impl("seedance-2.5", "x", {"durtion": 5}, dry_run=False)
    with pytest.raises(UsageError, match="te falta la @"):
        m.generate_impl("seedance-2.5-i2v", "x", {"image_url": "frame.png"}, dry_run=False)


def test_submit_returns_request_id_and_logs_via_mcp(home, monkeypatch):
    sent = fake_submit(monkeypatch)
    out = m.generate_impl("z-image", "apple", {"aspect_ratio": "1:1"}, dry_run=False)
    assert out["submitted"] is True and out["request_id"] == RID and len(sent) == 1
    rec = storage.read_history(home / "output" / "history.jsonl")[-1]
    assert rec["via"] == "mcp" and rec["fingerprint"] and rec["status"] == "queued"


def test_duplicate_within_window_is_refused(home, monkeypatch):
    sent = fake_submit(monkeypatch, [RID, "second-000000"])
    first = m.generate_impl("z-image", "apple", None, dry_run=False)
    again = m.generate_impl("z-image", "apple", None, dry_run=False)
    assert first["submitted"] and again["refused"] == "duplicate" and again["request_id"] == RID
    assert len(sent) == 1, "a retried identical call must not pay twice"
    forced = m.generate_impl("z-image", "apple", None, dry_run=False, allow_duplicate=True)
    assert forced["submitted"] and len(sent) == 2
    other = m.generate_impl("z-image", "pear", None, dry_run=False)
    assert other["submitted"]


def test_daily_cap(home, monkeypatch):
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "2")
    sent = fake_submit(monkeypatch, ["a0000001", "a0000002", "a0000003"])
    assert m.generate_impl("z-image", "one", None, dry_run=False)["submitted"]
    assert m.generate_impl("z-image", "two", None, dry_run=False)["submitted"]
    third = m.generate_impl("z-image", "three", None, dry_run=False)
    assert third["refused"] == "daily_limit" and third["limit"] == 2 and len(sent) == 2


def test_rejected_submit_reports_error(home, monkeypatch):
    import higgsfield_client
    import httpx

    def submit(endpoint, params):
        err = higgsfield_client.HiggsfieldClientError("Not enough credits")
        err.__cause__ = httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://a"),
                                              response=httpx.Response(403))
        raise err
    monkeypatch.setattr(api, "submit", submit)
    out = m.generate_impl("z-image", "apple", None, dry_run=False)
    assert out["submitted"] is False and "HTTP 403" in out["error"]


# ---------- wait ----------

def test_wait_in_progress_returns_not_done_without_final_record(home, monkeypatch):
    fake_submit(monkeypatch)
    m.generate_impl("z-image", "apple", None, dry_run=False)
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: {"status": "in_progress"})
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    data, _ = m.wait_impl(RID, max_seconds=30)
    assert data["done"] is False and data["status"] == "in_progress"
    assert storage.read_history(home / "output" / "history.jsonl")[-1]["status"] == "queued"


def test_wait_completes_downloads_and_previews(home, media_server, monkeypatch):
    fake_submit(monkeypatch)
    m.generate_impl("z-image", "apple", None, dry_run=False)
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: {"status": "completed", "images": [{"url": f"{media_server}/img.png"}]})
    data, outputs = m.wait_impl(RID)
    assert data["done"] and data["ok"] and data["label"] == "z-image"
    assert outputs[0]["file"].endswith(f"z-image_{RID}.png")
    previews = m._previews(outputs)
    assert len(previews) == 1 and previews[0].data[:2] == b"\xff\xd8"  # JPEG thumbnail
    # second call is served from history, no new download or upload
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: pytest.fail("must not poll again"))
    again, _ = m.wait_impl(RID)
    assert again["cached"] is True and again["outputs"][0]["file"] == outputs[0]["file"]


def test_wait_unknown_request_fails_fast(home, monkeypatch):
    import higgsfield_client
    import httpx
    calls = []

    def get_json(url, timeout=None):
        calls.append(url)
        err = higgsfield_client.HiggsfieldClientError("not found")
        err.__cause__ = httpx.HTTPStatusError("x", request=httpx.Request("GET", url), response=httpx.Response(404))
        raise err
    monkeypatch.setattr(api, "get_json", get_json)
    data, _ = m.wait_impl("abcdef12-0000")
    assert data["done"] is False and "HTTP 404" in data["error"] and len(calls) == 3  # 404 gets 3 tries


def test_wait_rejects_bad_id(home):
    with pytest.raises(UsageError):
        m.wait_impl("../../etc")


# ---------- install into Claude config ----------

def test_install_merges_and_backs_up(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}, "other": 1}))
    assert cli.install_mcp(cfg) == "añadido"
    data = json.loads(cfg.read_text())
    assert data["other"] == 1 and data["mcpServers"]["github"] == {"command": "x"}
    assert data["mcpServers"]["higgsfield"] == {"command": sys.executable, "args": ["-m", "hfcli", "mcp"]}
    assert list(tmp_path.glob("claude_desktop_config.json.bak-*"))
    assert cli.install_mcp(cfg) == "actualizado"


def test_install_refuses_broken_json(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{not json")
    with pytest.raises(UsageError):
        cli.install_mcp(cfg)
    assert cfg.read_text() == "{not json"


# ---------- real stdio round-trip ----------

def test_stdio_roundtrip(tmp_path):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "HFCLI_HOME": str(tmp_path), "HF_KEY": "test-id:test-secret-value"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "hfcli", "mcp"], env=env)

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                listing = await session.call_tool("hf_models", {"kind": "video", "search": "seedance-2.5"})
                info = await session.call_tool("hf_model_info", {"model": "kling-3-i2v"})
                dry = await session.call_tool("hf_generate", {"model": "z-image", "prompt": "apple",
                                                              "params": {"aspect_ratio": "1:1"}})
                bad = await session.call_tool("hf_generate", {"model": "z-image", "prompt": "apple",
                                                              "params": {"style": "x"}})
                hist = await session.call_tool("hf_history", {})
                return init, tools, listing, info, dry, bad, hist

    init, tools, listing, info, dry, bad, hist = anyio.run(scenario)
    assert init.serverInfo.name == "higgsfield" and "dinero" in (init.instructions or "")
    assert set(tools) == {"hf_models", "hf_model_info", "hf_generate", "hf_wait", "hf_history", "hf_cancel",
                          "hf_doctor", "hf_spend"}
    assert tools["hf_models"].annotations.readOnlyHint is True
    assert tools["hf_generate"].annotations.readOnlyHint is False
    assert "seedance-2.5-i2v" in listing.content[0].text
    assert "image_url" in info.content[0].text
    dry_data = json.loads(dry.content[0].text)
    assert dry_data["dry_run"] is True and dry_data["params"]["aspect_ratio"] == "1:1"
    assert "style" in bad.content[0].text and "Error" in bad.content[0].text
    assert json.loads(hist.content[0].text) == []
    assert not (tmp_path / "output" / "history.jsonl").exists(), "nothing was submitted"


# ---------- regression tests for the second review ----------

def _slow_submit(monkeypatch, delay=0.5):
    sent, lock = [], threading.Lock()

    def submit(endpoint, params):
        with lock:
            sent.append(params)
            n = len(sent)
        REAL_SLEEP(delay)
        return {"request_id": f"{n:08d}-par", "status_url": "https://platform.higgsfield.ai/requests/x/status"}
    monkeypatch.setattr(api, "submit", submit)
    return sent


def test_parallel_identical_calls_pay_once(home, monkeypatch):
    sent = _slow_submit(monkeypatch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(m.generate_impl("z-image", "apple", None, dry_run=False)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1, "only one of four simultaneous identical calls may be sent"
    assert sum(1 for r in results if r.get("submitted") is True) == 1
    assert sum(1 for r in results if r.get("refused") == "duplicate") == 3


def test_uncertain_submit_blocks_identical_retry_and_counts(home, monkeypatch):
    import httpx
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "2")
    calls = []

    def submit(endpoint, params):
        calls.append(params)
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(api, "submit", submit)
    first = m.generate_impl("z-image", "apple", None, dry_run=False)
    assert first["submitted"] == "unknown" and first["status"] == "uncertain"
    retry = m.generate_impl("z-image", "apple", None, dry_run=False)
    assert retry["refused"] == "duplicate" and retry["state"] == "uncertain" and len(calls) == 1
    m.generate_impl("z-image", "pear", None, dry_run=False)
    capped = m.generate_impl("z-image", "plum", None, dry_run=False)
    assert capped["refused"] == "daily_limit" and len(calls) == 2


def test_clean_rejection_releases_the_reservation(home, monkeypatch):
    import higgsfield_client
    import httpx
    state = {"fail": True}

    def submit(endpoint, params):
        if state["fail"]:
            err = higgsfield_client.HiggsfieldClientError("Not enough credits")
            err.__cause__ = httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://a"),
                                                  response=httpx.Response(403))
            raise err
        return {"request_id": RID, "status_url": "https://platform.higgsfield.ai/requests/x/status"}
    monkeypatch.setattr(api, "submit", submit)
    assert m.generate_impl("z-image", "apple", None, dry_run=False)["submitted"] is False
    state["fail"] = False  # user topped up credits: the same request may be sent now
    assert m.generate_impl("z-image", "apple", None, dry_run=False)["submitted"] is True


def test_unwritable_ledger_sends_nothing(home, monkeypatch):
    monkeypatch.setattr(api, "submit", lambda *a: pytest.fail("must not submit"))

    def boom(settings, record):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(m, "_ledger_append", boom)
    out = m.generate_impl("z-image", "apple", None, dry_run=False)
    assert out["refused"] == "ledger_unwritable"


def test_normalised_fingerprint_catches_rewordings(home, monkeypatch):
    sent = fake_submit(monkeypatch, [RID, "second-000000"])
    assert m.generate_impl("z-image", "apple", None, dry_run=False)["submitted"]
    # same request with an explicit schema default and a trailing space
    again = m.generate_impl("z-image", "apple ", {"aspect_ratio": "1:1"}, dry_run=False)
    assert again["refused"] == "duplicate" and len(sent) == 1


def test_naive_and_old_ledger_entries(home, monkeypatch):
    settings = m._settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    old = (m.datetime.now() - m.timedelta(hours=25)).isoformat()
    recent_naive = (m.datetime.now() - m.timedelta(hours=23)).replace(microsecond=0).isoformat()
    with m.ledger_path(settings).open("w") as fh:
        fh.write(json.dumps({"ts": old, "reservation": "old1", "state": "submitted", "fingerprint": "x"}) + "\n")
        fh.write(json.dumps({"ts": recent_naive, "reservation": "new1", "state": "submitted", "fingerprint": "y"}) + "\n")
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "2")
    fake_submit(monkeypatch, ["a0000001", "a0000002"])
    assert m.generate_impl("z-image", "one", None, dry_run=False)["submitted"]  # 25 h-old entry no longer counts
    assert m.generate_impl("z-image", "two", None, dry_run=False)["refused"] == "daily_limit"


def test_wait_respects_deadline_on_slow_errors(home, monkeypatch):
    import httpx

    def slow_fail(url, timeout=None):
        REAL_SLEEP(1)
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(api, "get_json", slow_fail)
    t0 = time.monotonic()
    data, _ = m.wait_impl("abcdef12-0001", max_seconds=5)
    assert data["done"] is False and time.monotonic() - t0 < 8


def test_slow_storing_returns_then_completes_once(home, media_server, monkeypatch):
    fake_submit(monkeypatch)
    m.generate_impl("z-image", "apple", None, dry_run=False)
    monkeypatch.setattr(api, "get_json", lambda url, timeout=None: {"status": "completed",
                                                                    "images": [{"url": f"{media_server}/img.png"}]})
    real_download, downloads = storage.download, []

    def slow_download(*a, **k):
        downloads.append(1)
        REAL_SLEEP(6)
        return real_download(*a, **k)
    monkeypatch.setattr(storage, "download", slow_download)
    first, _ = m.wait_impl(RID, max_seconds=5)
    assert first["done"] is False and first["status"] == "storing"
    second, outputs = m.wait_impl(RID, max_seconds=10)
    assert second["done"] is True and second["ok"] and len(downloads) == 1
    assert Path(outputs[0]["file"]).exists()


def test_install_keeps_mode_follows_symlink_and_unique_backups(tmp_path):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"mcpServers": {"github": {"command": "x"}}}))
    os.chmod(real, 0o600)
    link = tmp_path / "claude_desktop_config.json"
    link.symlink_to(real)
    cli.install_mcp(link)
    cli.install_mcp(link)
    assert link.is_symlink(), "the symlink must survive"
    data = json.loads(real.read_text())
    assert "higgsfield" in data["mcpServers"] and "github" in data["mcpServers"]
    assert oct(real.stat().st_mode & 0o777) == "0o600"
    backups = list(real.parent.glob("claude.json.bak-*"))
    assert len(backups) == 2 and all(oct(b.stat().st_mode & 0o777) == "0o600" for b in backups)


def test_install_refuses_without_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "mcp_available", lambda: False)
    cfg = tmp_path / "c.json"
    with pytest.raises(UsageError, match="MCP"):
        cli.install_mcp(cfg)
    assert not cfg.exists()


# ---------- spend control (no default cap, estimates, report) ----------

def test_no_cap_by_default(home, monkeypatch):
    sent = fake_submit(monkeypatch, [f"{i:08d}-nocap" for i in range(1, 31)])
    for i in range(25):
        assert m.generate_impl("z-image", f"prompt {i}", None, dry_run=False)["submitted"] is True
    assert len(sent) == 25


def test_dry_run_and_submit_carry_estimate(home, monkeypatch):
    dry = m.generate_impl("seedance-2.5-i2v", "push-in", {"image_url": "https://a/b.png", "duration": 4, "resolution": "480p"})
    assert dry["estimated_cost_usd"] == 0.8224 and "480p" in dry["cost_note"]
    assert m.generate_impl("z-image", "apple")["estimated_cost_usd"] is None
    fake_submit(monkeypatch)
    out = m.generate_impl("seedance-2.5", "sunset", {"duration": 5}, dry_run=False)
    assert out["estimated_cost_usd"] == round(0.4622 * 5, 4)  # default resolution 720p
    rec = storage.read_history(home / "output" / "history.jsonl")[-1]
    assert rec["est_usd"] == round(0.4622 * 5, 4)


def test_spend_tool_reports(home, monkeypatch):
    fake_submit(monkeypatch, ["a0000001", "a0000002"])
    m.generate_impl("seedance-2.5", "one", {"duration": 4, "resolution": "480p"}, dry_run=False)
    m.generate_impl("z-image", "two", None, dry_run=False)
    report = m.spend_impl(1)
    assert report["total"]["lanzadas"] == 2 and report["total"]["pendientes_o_inciertas"] == 2
