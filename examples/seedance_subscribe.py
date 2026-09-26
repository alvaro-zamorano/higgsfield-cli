"""Generate a video with Seedance 2.5 (text-to-video) via the official Higgsfield Python SDK.

Credentials: HF_KEY="key-id:key-secret" in .env.local (never committed, never printed).
Exit codes: 0 = video URL returned, 1 = request did not succeed, 2 = local config error.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

MODEL = "bytedance/seedance-2.5/text-to-video"
ARGUMENTS = {
    "prompt": "A cinematic scene at sunset",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
}

PROJECT_DIR = Path(os.getenv("HFCLI_HOME") or Path.home() / ".higgsfield-cli")  # holds .env.local
RESULT_FILE = PROJECT_DIR / "output" / "example_last_result.json"


def load_credentials() -> bool:
    """Load HF_KEY from .env.local and check its shape without revealing it."""
    load_dotenv(PROJECT_DIR / ".env.local", override=False)
    key_id, sep, key_secret = os.getenv("HF_KEY", "").strip().partition(":")
    if sep and key_id and key_secret:
        return True
    print(
        "Config error: HF_KEY is missing or not in key-id:key-secret format "
        "in .env.local (value not shown)."
    )
    return False


def save_result(payload: dict) -> None:
    """Keep the API response (status, request_id, output URLs) for later inspection."""
    RESULT_FILE.parent.mkdir(exist_ok=True)
    RESULT_FILE.write_text(json.dumps(payload, indent=2))


def main() -> int:
    if not load_credentials():
        return 2

    # Imported after loading .env.local so the SDK finds HF_KEY in the environment.
    import httpx
    import higgsfield_client

    request_ids: list[str] = []
    last_status: list[str] = []

    def on_enqueue(request_id: str) -> None:
        request_ids.append(request_id)
        print(f"Submitted: request_id={request_id}", flush=True)

    def on_queue_update(status: object) -> None:
        name = type(status).__name__
        if not last_status or last_status[-1] != name:
            last_status.append(name)
            print(f"Status: {name}", flush=True)

    try:
        result = higgsfield_client.subscribe(
            MODEL,
            arguments=ARGUMENTS,
            on_enqueue=on_enqueue,
            on_queue_update=on_queue_update,
        )
    except KeyboardInterrupt:
        rid = request_ids[0] if request_ids else "not assigned yet"
        print(f"Interrupted locally; the request may still run (request_id={rid}).")
        return 1
    except higgsfield_client.CredentialsMissedError as exc:
        print(f"Config error: {exc}")
        return 2
    except higgsfield_client.HiggsfieldClientError as exc:
        # HTTP errors from the API: invalid credentials, no credits, invalid input...
        cause = exc.__cause__
        code = getattr(getattr(cause, "response", None), "status_code", "?")
        print(f"API error (HTTP {code}): {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"Network error talking to Higgsfield: {type(exc).__name__}: {exc}")
        return 1
    except ValueError as exc:  # SDK raises this for an unrecognised status value
        print(f"Unexpected response from the API: {exc}")
        return 1

    save_result(result)
    status = result.get("status")
    request_id = result.get("request_id") or (request_ids[0] if request_ids else "?")

    if status == "completed":
        video_url = (result.get("video") or {}).get("url")
        if not video_url:
            print(f"Completed but no video URL in the response (request_id={request_id}).")
            return 1
        print(f"Completed (request_id={request_id})")
        print(f"Video URL: {video_url}")
        return 0

    if status == "nsfw":
        print(f"Rejected by content moderation (request_id={request_id}). No video generated.")
    elif status == "failed":
        detail = result.get("error") or "no error detail returned"
        print(f"Generation failed (request_id={request_id}): {detail}")
    elif status == "canceled":
        print(f"Request was canceled before processing (request_id={request_id}).")
    else:
        print(f"Unexpected final status {status!r} (request_id={request_id}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
