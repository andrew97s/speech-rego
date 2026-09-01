"""
Windows 客户端调用 GPU 识别服务的 HTTP 封装。

说完一句后把 16 kHz PCM 发给 asr_server.py 的 POST /v1/recognize。
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


class RemoteAsrError(Exception):
    """远程识别失败（网络、鉴权、服务端错误）。"""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = int(status)


def asr_remote_cfg(config: dict) -> dict:
    return dict(config.get("asr_remote") or {})


def remote_asr_enabled(config: dict) -> bool:
    r = asr_remote_cfg(config)
    url = str(r.get("url") or "").strip()
    if not url:
        return False
    return bool(r.get("enabled", True))


def _health_url(recognize_url: str) -> str:
    raw = recognize_url.rstrip("/")
    if raw.endswith("/v1/recognize"):
        return raw[: -len("/v1/recognize")] + "/v1/health"
    if raw.endswith("/recognize"):
        return raw[: -len("/recognize")] + "/health"
    return raw.rsplit("/", 1)[0] + "/health"


def check_remote_asr(config: dict, timeout_sec: float = 5.0) -> tuple[bool, str]:
    """探测识别服务 /health。返回 (ok, detail)。"""
    r = asr_remote_cfg(config)
    url = str(r.get("url") or "").strip()
    if not url:
        return False, "asr_remote.url 未配置"
    health = _health_url(url)
    token = str(r.get("token") or "").strip()
    req = urllib.request.Request(health, method="GET")
    if token:
        req.add_header("X-ASR-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            if data.get("ok"):
                model = data.get("model") or ""
                device = data.get("device") or ""
                return True, f"{health} 就绪 model={model} device={device}".strip()
            return False, body[:200] or "health 返回 ok=false"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def transcribe_remote(
    pcm16_mono: bytes,
    config: dict,
    *,
    language: str = "中文",
    hotwords: Optional[Sequence[str]] = None,
    sample_rate: int = _SAMPLE_RATE,
) -> str:
    """把整段 PCM 交给远程 FunASR，返回原始识别文本。"""
    r = asr_remote_cfg(config)
    url = str(r.get("url") or "").strip()
    if not url:
        raise RemoteAsrError("asr_remote.url is empty")
    timeout = float(r.get("timeout_sec", 60))
    token = str(r.get("token") or "").strip()
    payload = {
        "audio_b64": base64.b64encode(pcm16_mono).decode("ascii"),
        "encoding": "pcm_s16le",
        "sample_rate": int(sample_rate),
        "language": language,
        "itn": True,
        "hotwords": [str(h).strip() for h in (hotwords or []) if h and str(h).strip()],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-ASR-Token", token)
    logger.info(
        "[remote-asr] POST %s (%.2f s audio, timeout=%ss)",
        url,
        len(pcm16_mono) / (sample_rate * 2),
        timeout,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = exc.reason
        raise RemoteAsrError(
            f"ASR server HTTP {exc.code}: {detail or exc.reason}",
            status=int(exc.code),
        ) from exc
    except urllib.error.URLError as exc:
        raise RemoteAsrError(f"Cannot reach ASR server: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RemoteAsrError(f"ASR server timed out after {timeout}s") from exc

    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RemoteAsrError(f"ASR server returned non-JSON: {body[:200]}") from exc
    if not isinstance(parsed, dict):
        raise RemoteAsrError("ASR server JSON is not an object")
    if parsed.get("ok") is False:
        raise RemoteAsrError(str(parsed.get("error") or parsed.get("message") or "ok=false"))
    return str(parsed.get("text") or "").strip()
