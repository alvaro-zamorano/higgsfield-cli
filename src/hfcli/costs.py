"""Cost estimates from Higgsfield's published per-second prices (before discounts).

Only models whose public pricing can be computed from the request are covered; everything else
returns None ("precio no publicado"). The real amount is always the one in open.higgsfield.ai/usage.
Failed and NSFW generations are not charged (Higgsfield API FAQ).
"""
from __future__ import annotations

from hfcli.catalog import Target

# USD per generated second, by resolution. Source: pricing section of each model's public docs.
PER_SECOND: dict[str, dict[str, float]] = {
    "alibaba/wan-3.0/text-to-video": {"480p": 0.05, "720p": 0.10, "1080p": 0.20},
    "alibaba/wan-3.0/image-to-video": {"480p": 0.05, "720p": 0.10, "1080p": 0.20},
    "alibaba/wan-3.0/reference-to-video": {"480p": 0.05, "720p": 0.10, "1080p": 0.20},
    # Seedance 2.5: published "roughly" rates for 16:9 output without video input (token-metered).
    "bytedance/seedance-2.5/text-to-video": {"480p": 0.2056, "720p": 0.4622, "1080p": 1.1372},
    "bytedance/seedance-2.5/image-to-video": {"480p": 0.2056, "720p": 0.4622, "1080p": 1.1372},
    "bytedance/seedance-2.5/reference-to-video": {"480p": 0.2056, "720p": 0.4622, "1080p": 1.1372},
}
MINIMAX_REF = "minimax/h3/reference-to-video"  # $0.13/s, first 5 reference images included, +$0.08 each


def _param(target: Target, params: dict, name: str):
    if name in params:
        return params[name]
    return target.properties.get(name, {}).get("default")


def estimate(target: Target, params: dict) -> tuple[float | None, str]:
    """(usd, note). usd is None when the price can't be derived from public pricing."""
    endpoint = target.endpoint
    duration = _param(target, params, "duration")
    if endpoint in PER_SECOND:
        if params.get("video_urls") or params.get("video_url"):
            return None, "con vídeo de entrada el precio depende de su duración"
        resolution = _param(target, params, "resolution")
        rate = PER_SECOND[endpoint].get(str(resolution))
        if rate is None or not isinstance(duration, (int, float)):
            return None, "resolución o duración sin precio publicado"
        note = "aprox. (tarifa 16:9)" if endpoint.startswith("bytedance/") else "tarifa publicada"
        return round(rate * float(duration), 4), f"{duration} s × ${rate}/s a {resolution}, {note}, antes de descuentos"
    if endpoint == MINIMAX_REF and isinstance(duration, (int, float)):
        extra = max(0, len(params.get("image_urls") or []) - 5)
        usd = 0.13 * float(duration) + 0.08 * extra
        return round(usd, 4), f"{duration} s × $0.13/s + {extra} referencias extra × $0.08, antes de descuentos"
    return None, "precio no publicado para este modelo"


def describe(usd: float | None, note: str) -> str:
    return f"~${usd:.2f} ({note})" if usd is not None else f"desconocido: {note}"


CHARGED = {"completed"}
NOT_CHARGED = {"failed", "nsfw", "canceled"}  # per Higgsfield's FAQ, failed and NSFW are not charged


def spend_report(history: list[dict], days: float, now=None) -> dict:
    """Aggregate the local history over the last `days` days. Amounts are estimates."""
    from datetime import datetime, timedelta

    now = now or datetime.now().astimezone()
    since = now - timedelta(days=days)
    by_model: dict[str, dict] = {}
    for rec in history:
        try:
            ts = datetime.fromisoformat(str(rec.get("ts")))
        except ValueError:
            continue
        ts = ts if ts.tzinfo else ts.astimezone()
        if ts < since or rec.get("event") not in ("submitted", "final", "submit_uncertain"):
            continue
        row = by_model.setdefault(rec.get("label") or rec.get("endpoint") or "?", {
            "lanzadas": 0, "completadas": 0, "no_cobradas": 0, "pendientes_o_inciertas": 0,
            "usd_estimado": 0.0, "completadas_sin_precio": 0})
        row["lanzadas"] += 1
        status = rec.get("status")
        est = rec.get("est_usd") if "est_usd" in rec else _estimate_record(rec)
        if status in CHARGED:
            row["completadas"] += 1
            if isinstance(est, (int, float)):
                row["usd_estimado"] = round(row["usd_estimado"] + est, 4)
            else:
                row["completadas_sin_precio"] += 1
        elif status in NOT_CHARGED:
            row["no_cobradas"] += 1
        else:  # queued, in_progress, timeout, lost, interrupted, uncertain, error after submit
            row["pendientes_o_inciertas"] += 1
    total = {k: sum(r[k] for r in by_model.values()) for k in
             ("lanzadas", "completadas", "no_cobradas", "pendientes_o_inciertas", "completadas_sin_precio")}
    total["usd_estimado"] = round(sum(r["usd_estimado"] for r in by_model.values()), 4)
    return {"dias": days, "desde": since.isoformat(timespec="minutes"), "por_modelo": by_model, "total": total,
            "nota": "Estimación local con precios públicos antes de descuentos. Los fallos y NSFW no se cobran. "
                    "Importe real: open.higgsfield.ai/usage"}


def _estimate_record(rec: dict) -> float | None:
    """Records written before estimates existed: recompute from endpoint + params when possible."""
    from hfcli.catalog import resolve
    from hfcli.config import UsageError
    try:
        target = resolve(rec.get("endpoint") or "")
    except UsageError:
        return None
    return estimate(target, rec.get("params") or {})[0]
