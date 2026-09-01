#!/usr/bin/env python3
"""
Fun-ASR-Nano HTTP 识别服务（部署在 GPU 服务器上）。

不采麦克风。接收客户端提交的一整句 PCM/WAV，返回识别文本。

Usage:
  python asr_server.py
  python asr_server.py --config asr_server.json

Endpoints:
  GET  /v1/health
  POST /v1/recognize   JSON {audio_b64, encoding, sample_rate, language, hotwords}
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from funasr_asr import (
    FunASRRuntime,
    asr_language_from_config,
    funasr_cfg,
    hotword_list_from_config,
    load_funasr_runtime,
)

logger = logging.getLogger("asr_server")

_MAX_BODY = 8 * 1024 * 1024
_SAMPLE_RATE = 16000

_DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 8767,
    "token": "",
    "log_level": "INFO",
    "funasr": {
        "asr_model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "vad_model": "fsmn-vad",
        "punc_model": "",
        "device": "cuda",
        "ncpu": 4,
        "cache_dir": "models/funasr",
        "disable_update": True,
        "hub": "ms",
        "trust_remote_code": False,
        "language": "中文",
        "itn": True,
        "hotword": "",
    },
    "whisper": {
        "language": "zh",
        "domain_keywords": [],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k.startswith("_"):
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_asr_server_config(path: str) -> dict:
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        logger.warning("%s not found — using built-in defaults", abs_path)
        return dict(_DEFAULTS)
    with open(abs_path, encoding="utf-8-sig") as f:
        user = json.load(f)
    return _deep_merge(_DEFAULTS, user)


class AsrService:
    """进程内单例：加载 Nano，串行 generate。"""

    def __init__(self, config: dict):
        self.config = config
        self.runtime: Optional[FunASRRuntime] = None
        self._lock = threading.Lock()

    def load(self) -> None:
        logger.info("Loading Fun-ASR-Nano (this may take a while on first run)…")
        self.runtime = load_funasr_runtime(
            self.config,
            base_dir=Path(__file__).resolve().parent,
            components=("asr",),
        )
        logger.info(
            "ASR ready — model=%s device=%s",
            self.runtime.asr_name,
            self.runtime.device,
        )

    def health(self) -> dict:
        rt = self.runtime
        ready = rt is not None and rt.asr_model is not None
        return {
            "ok": ready,
            "model": rt.asr_name if rt else "",
            "device": rt.device if rt else "",
            "ts": time.time(),
        }

    def recognize(
        self,
        pcm16: bytes,
        *,
        language: str = "",
        hotwords: Optional[list] = None,
        itn: Optional[bool] = None,
    ) -> dict:
        rt = self.runtime
        if rt is None or rt.asr_model is None:
            raise RuntimeError("ASR model is not loaded")
        if itn is not None:
            rt.itn = bool(itn)
        if language:
            rt.language = language
        if hotwords is not None:
            rt.hotwords = [str(h).strip() for h in hotwords if h and str(h).strip()]
        t0 = time.time()
        with self._lock:
            text = rt.transcribe_pcm(pcm16)
        elapsed = time.time() - t0
        dur_s = len(pcm16) / (_SAMPLE_RATE * 2)
        logger.info(
            "recognized %.2fs audio in %.2fs → %s",
            dur_s,
            elapsed,
            (text[:80] + "…") if len(text) > 80 else text,
        )
        return {
            "ok": True,
            "text": text,
            "duration_s": round(dur_s, 3),
            "elapsed_s": round(elapsed, 3),
        }


def _check_token(handler: BaseHTTPRequestHandler, expected: str) -> bool:
    if not expected:
        return True
    got = handler.headers.get("X-ASR-Token") or ""
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    return got == expected


def _pcm_from_payload(data: dict) -> bytes:
    b64 = data.get("audio_b64") or data.get("audio") or ""
    if not b64:
        raise ValueError("missing audio_b64")
    raw = base64.b64decode(b64)
    enc = str(data.get("encoding") or "pcm_s16le").strip().lower()
    if enc in ("pcm_s16le", "pcm", "s16le", "linear16"):
        return raw
    if enc in ("wav", "wave"):
        if len(raw) < 44 or raw[:4] != b"RIFF":
            raise ValueError("encoding=wav but body is not a WAV")
        # skip header if standard 44-byte PCM wav
        return raw[44:] if raw[36:40] == b"data" else raw
    raise ValueError(f"unsupported encoding: {enc}")


def make_handler(service: AsrService, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s - " + fmt, self.address_string(), *args)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ASR-Token, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in ("/health", "/v1/health"):
                if not _check_token(self, token):
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                self._send_json(200, service.health())
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in ("/recognize", "/v1/recognize"):
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            if not _check_token(self, token):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_BODY:
                self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                return
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "body must be JSON"})
                return
            if not isinstance(data, dict):
                self._send_json(400, {"ok": False, "error": "JSON object required"})
                return
            try:
                pcm = _pcm_from_payload(data)
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            sr = int(data.get("sample_rate") or _SAMPLE_RATE)
            if sr != _SAMPLE_RATE:
                self._send_json(400, {"ok": False, "error": f"sample_rate must be {_SAMPLE_RATE}"})
                return
            if len(pcm) < _SAMPLE_RATE:  # < 0.5 s
                self._send_json(400, {"ok": False, "error": "audio too short"})
                return
            language = str(data.get("language") or "").strip()
            if not language:
                language = asr_language_from_config(service.config)
            hotwords = data.get("hotwords")
            if not isinstance(hotwords, list):
                hotwords = hotword_list_from_config(service.config)
            itn = data.get("itn")
            try:
                result = service.recognize(
                    pcm, language=language, hotwords=hotwords, itn=itn,
                )
            except Exception as exc:
                logger.exception("recognize failed")
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, result)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Fun-ASR-Nano HTTP recognition server")
    parser.add_argument(
        "--config",
        default=os.environ.get("SPEECH_REGO_ASR_CONFIG", "asr_server.json"),
        help="config JSON path (default asr_server.json)",
    )
    args = parser.parse_args()

    app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)
    config = load_asr_server_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(config.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    host = str(config.get("host") or "0.0.0.0")
    port = int(config.get("port") or 8767)
    token = str(config.get("token") or "").strip()
    fcfg = funasr_cfg(config)

    border = "=" * 56
    logger.info(border)
    logger.info("  Fun-ASR-Nano recognition HTTP service")
    logger.info("  http://%s:%s/v1/recognize", host, port)
    logger.info("  model     : %s", fcfg.get("asr_model"))
    logger.info("  device    : %s", fcfg.get("device"))
    logger.info("  auth      : %s", "X-ASR-Token" if token else "off")
    logger.info(border)

    service = AsrService(config)
    service.load()

    httpd = ThreadingHTTPServer((host, port), make_handler(service, token))
    logger.info("Listening. Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
