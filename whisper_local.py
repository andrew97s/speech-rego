"""
Resolve faster-whisper models from HF_HOME offline cache (models/hf/hub/...).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_REPO_PREFIX = "models--Systran--faster-whisper-"


def _hf_hub_root(hf_home: Optional[str] = None) -> Optional[Path]:
    root = hf_home or os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not root:
        return None
    hub = Path(root) / "hub"
    return hub if hub.is_dir() else None


def _snapshot_has_weights(snap: Path) -> bool:
    if not snap.is_dir():
        return False
    for name in ("model.bin", "model.safetensors"):
        if (snap / name).is_file() and (snap / name).stat().st_size > 0:
            return True
    return False


def _newest_snapshot(snapshots_dir: Path) -> Optional[Path]:
    if not snapshots_dir.is_dir():
        return None
    cands = [p for p in snapshots_dir.iterdir() if p.is_dir() and _snapshot_has_weights(p)]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def list_bundled_whisper_models(hf_home: Optional[str] = None) -> List[str]:
    hub = _hf_hub_root(hf_home)
    if hub is None:
        return []
    out: List[str] = []
    for entry in hub.iterdir():
        if not entry.is_dir() or not entry.name.startswith(_REPO_PREFIX):
            continue
        name = entry.name[len(_REPO_PREFIX):]
        if _newest_snapshot(entry / "snapshots"):
            out.append(name)
    return sorted(out)


def resolve_whisper_snapshot(
    model_name: str,
    hf_home: Optional[str] = None,
) -> Optional[Path]:
    hub = _hf_hub_root(hf_home)
    if hub is None:
        return None
    snap = _newest_snapshot(hub / f"{_REPO_PREFIX}{model_name}" / "snapshots")
    return snap


def is_hub_offline() -> bool:
    v = os.environ.get("HF_HUB_OFFLINE", "")
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def read_bundled_model_manifest(hf_home: Optional[str] = None) -> Optional[str]:
    """models/bundled_whisper_model.txt written by build_offline.ps1."""
    root = hf_home or os.environ.get("HF_HOME")
    if not root:
        return None
    manifest = Path(root).parent / "bundled_whisper_model.txt"
    if not manifest.is_file():
        return None
    name = manifest.read_text(encoding="utf-8").strip()
    return name or None


def pick_whisper_model(
    requested: str,
    hf_home: Optional[str] = None,
) -> Tuple[str, Optional[Path]]:
    """
    Return (model_name, local_snapshot_path).
    If *requested* is missing locally but others exist, use the first bundled one.
    """
    requested = (requested or "base").strip()
    path = resolve_whisper_snapshot(requested, hf_home)
    if path is not None:
        return requested, path

    bundled = list_bundled_whisper_models(hf_home)
    if bundled:
        fallback = bundled[0]
        if fallback != requested:
            logger.warning(
                "Whisper model %r is not in the offline bundle; using %r instead. "
                "Set whisper.model to a bundled name in config.json or rebuild the package.",
                requested,
                fallback,
            )
        return fallback, resolve_whisper_snapshot(fallback, hf_home)

    return requested, None


def offline_model_error_message(requested: str, hf_home: Optional[str] = None) -> str:
    hub = _hf_hub_root(hf_home)
    hub_s = str(hub) if hub else "(HF_HOME not set)"
    bundled = list_bundled_whisper_models(hf_home)
    lines = [
        f"Cannot load Whisper model {requested!r} in offline mode (HF_HUB_OFFLINE is set).",
        f"HF cache hub: {hub_s}",
    ]
    if bundled:
        lines.append(f"Bundled models found: {', '.join(bundled)}")
        lines.append(f"Set whisper.model in config.json to one of: {', '.join(bundled)}")
    else:
        lines.append(
            "No Whisper weights under models/hf/hub/. Re-run build_offline.bat with the "
            "same model name, or copy a complete HF cache into models/hf."
        )
    lines.append(
        "To allow online download once, remove HF_HUB_OFFLINE from start_whisper.bat "
        "and restart (not recommended for production offline deploy)."
    )
    return " ".join(lines)
