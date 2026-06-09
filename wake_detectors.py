"""Sherpa-ONNX KWS 唤醒检测器。

流式 KeywordSpotter：PCM int16 → float32 → accept_waveform / decode_stream。
关键词需 token 化；未提供 keywords_file 时用 sherpa_onnx.utils.text2token 生成。
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


def _sherpa_keyword_tag(keyword: str) -> str:
    """Sherpa @ 后缀不能含空格，否则会被当成独立 token 导致 Encode 失败。"""
    return keyword.strip().replace(" ", "_")

def _sherpa_keywords_threshold(sensitivity: float, override: Optional[float]) -> float:
    if override is not None:
        return max(0.01, min(1.0, float(override)))
    s = max(0.0, min(1.0, float(sensitivity)))
    return max(0.08, min(0.40, 0.40 - s * 0.30))


def resolve_sherpa_model_paths(sherpa_cfg: dict, base_dir: Path) -> Dict[str, str]:
    """Resolve encoder/decoder/joiner/tokens/lexicon paths from sherpa_kws config."""
    model_dir = Path(str(sherpa_cfg.get("model_dir", ""))).expanduser()
    if not model_dir.is_absolute():
        model_dir = (base_dir / model_dir).resolve()

    if not model_dir.is_dir():
        raise FileNotFoundError(f"Sherpa KWS model_dir not found: {model_dir}")

    chunk = int(sherpa_cfg.get("chunk_size", 8))
    use_int8 = bool(sherpa_cfg.get("use_int8", True))
    epoch_tag = str(sherpa_cfg.get("epoch_tag", "epoch-13-avg-2"))
    cs = f"chunk-{chunk}-left-64"

    def _pick(prefix: str) -> Path:
        if use_int8 and prefix in ("encoder", "joiner"):
            int8 = model_dir / f"{prefix}-{epoch_tag}-{cs}.int8.onnx"
            if int8.is_file():
                return int8
        fp32 = model_dir / f"{prefix}-{epoch_tag}-{cs}.onnx"
        if fp32.is_file():
            return fp32
        raise FileNotFoundError(
            f"Sherpa KWS {prefix} model not found under {model_dir} (chunk={chunk})"
        )

    tokens = model_dir / "tokens.txt"
    if not tokens.is_file():
        raise FileNotFoundError(f"Sherpa KWS tokens.txt not found: {tokens}")

    lexicon_name = str(sherpa_cfg.get("lexicon", "") or "").strip()
    lexicon = model_dir / lexicon_name if lexicon_name else None

    return {
        "model_dir": str(model_dir),
        "encoder": str(_pick("encoder")),
        "decoder": str(_pick("decoder")),
        "joiner": str(_pick("joiner")),
        "tokens": str(tokens),
        "lexicon": str(lexicon) if lexicon and lexicon.is_file() else "",
    }


def build_sherpa_keywords_file(
    keywords: List[str],
    sherpa_cfg: dict,
    model_paths: Dict[str, str],
    cache_dir: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> str:
    """Return path to tokenized keywords.txt (cached or generated via sherpa-onnx-cli)."""
    explicit = str(sherpa_cfg.get("keywords_file", "") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        root = (base_dir or Path.cwd()).resolve()
        if not p.is_absolute():
            p = (root / p).resolve()
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"Sherpa keywords_file not found: {p}")

    if not keywords:
        raise ValueError("Sherpa KWS requires at least one keyword")

    tokens_type = str(sherpa_cfg.get("tokens_type", "ppinyin")).strip()
    key_src = "|".join(keywords) + "|" + tokens_type + "|" + model_paths["tokens"]
    digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:16]
    cache_root = cache_dir or (Path(model_paths["model_dir"]).parent / ".keywords-cache")
    cache_root.mkdir(parents=True, exist_ok=True)
    out_path = cache_root / f"keywords_{digest}.txt"
    if out_path.is_file() and out_path.stat().st_size > 0:
        return str(out_path)

    lexicon = model_paths.get("lexicon") or None
    if lexicon == "":
        lexicon = None

    try:
        from sherpa_onnx.utils import text2token
    except ImportError as exc:
        raise RuntimeError(
            "sherpa-onnx is required for keyword tokenization; "
            "pip install sherpa-onnx sentencepiece"
        ) from exc

    logger.info(
        "[WakeWord] Generating Sherpa keywords file (text2token): keywords=%s type=%s",
        keywords,
        tokens_type,
    )
    try:
        encoded = text2token(
            keywords,
            tokens=model_paths["tokens"],
            tokens_type=tokens_type,
            lexicon=lexicon,
        )
    except ImportError as exc:
        raise RuntimeError(
            "text2token requires sentencepiece (and pypinyin for Chinese): "
            "pip install sentencepiece pypinyin"
        ) from exc

    lines_written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for kw, toks in zip(keywords, encoded):
            if not toks:
                logger.warning("[WakeWord] text2token produced no tokens for %r", kw)
                continue
            tag = _sherpa_keyword_tag(kw)
            parts = [str(t) for t in toks] + [f"@{tag}"]
            f.write(" ".join(parts) + "\n")
            lines_written += 1

    if lines_written == 0:
        raise RuntimeError(
            f"text2token produced no keywords; check tokens_type={tokens_type!r} "
            f"and lexicon={lexicon!r}"
        )
    return str(out_path)


class SherpaKWSWakeWordDetector:
    """
    sherpa-onnx KeywordSpotter 流式唤醒。

    process() 接收 int16 PCM；命中后 debounce_sec 内不再触发。
    last_score 在命中时固定为 1.0（Sherpa 不暴露置信度）。
    """

    def __init__(
        self,
        keywords: List[str],
        sample_rate: int = _SAMPLE_RATE,
        *,
        sherpa_cfg: dict,
        sensitivity: float = 0.5,
        debounce_sec: float = 0.8,
        base_dir: Optional[Path] = None,
    ):
        if sample_rate != _SAMPLE_RATE:
            raise ValueError(f"Sherpa KWS requires {_SAMPLE_RATE} Hz, got {sample_rate}")

        import sherpa_onnx

        self.keywords = [k.strip() for k in keywords if k.strip()]
        if not self.keywords:
            raise ValueError("Sherpa KWS requires at least one keyword")

        root = Path(base_dir or Path.cwd()).resolve()
        cfg = dict(sherpa_cfg or {})
        self._debounce_sec = max(0.0, float(debounce_sec))
        self._last_wake = 0.0
        self._last_score = 0.0
        self._paused = False
        self._keyword_map: Dict[str, str] = {}

        model_paths = resolve_sherpa_model_paths(cfg, root)
        keywords_file = build_sherpa_keywords_file(
            self.keywords,
            cfg,
            model_paths,
            cache_dir=root / ".cache" / "sherpa-kws",
            base_dir=root,
        )
        self._parse_keyword_map(keywords_file)

        threshold_override = cfg.get("keywords_threshold")
        if threshold_override in ("", None):
            threshold_override = None
        else:
            threshold_override = float(threshold_override)

        keywords_threshold = _sherpa_keywords_threshold(sensitivity, threshold_override)
        provider = str(cfg.get("provider", "cpu")).strip().lower() or "cpu"
        num_threads = max(1, int(cfg.get("num_threads", 2)))

        logger.info("kws keywords_threshold : %s" , keywords_threshold)
        logger.info("kws keywords_score : %s" , float(cfg.get("keywords_score", 1.0)))
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=model_paths["tokens"],
            encoder=model_paths["encoder"],
            decoder=model_paths["decoder"],
            joiner=model_paths["joiner"],
            num_threads=num_threads,
            max_active_paths=max(1, int(cfg.get("max_active_paths", 4))),
            keywords_file=keywords_file,
            keywords_score=float(cfg.get("keywords_score", 1.0)),
            keywords_threshold=0.1,
            num_trailing_blanks=max(0, int(cfg.get("num_trailing_blanks", 1))),
            provider=provider,
        )
        self._stream = self._spotter.create_stream()
        self._sample_rate = sample_rate

        logger.info(
            "[WakeWord] Sherpa KWS — keywords=%s file=%s threshold=%.2f provider=%s threads=%d",
            self.keywords,
            keywords_file,
            keywords_threshold,
            provider,
            num_threads,
        )

    def _parse_keyword_map(self, keywords_file: str) -> None:
        """Map detected token lines back to user-facing keyword text (@suffix)."""
        try:
            for line in Path(keywords_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "@" in line:
                    display = line.rsplit("@", 1)[-1].strip()
                    if display:
                        self._keyword_map[display] = display
                        self._keyword_map[display.lower()] = display
                parts = line.split()
                if parts:
                    tail = parts[-1]
                    if tail.startswith("@"):
                        display = tail[1:]
                        if display:
                            self._keyword_map[display] = display
        except OSError as exc:
            logger.debug("[WakeWord] keyword map parse skipped: %s", exc)

        for kw in self.keywords:
            tag = _sherpa_keyword_tag(kw)
            self._keyword_map.setdefault(kw, kw)
            self._keyword_map.setdefault(kw.lower(), kw)
            if tag != kw:
                self._keyword_map.setdefault(tag, kw)
                self._keyword_map.setdefault(tag.lower(), kw)

    @property
    def last_score(self) -> float:
        return self._last_score

    def reset(self):
        self._spotter.reset_stream(self._stream)
        self._last_score = 0.0

    def pause(self):
        self._paused = True
        self.reset()

    def resume(self):
        self._paused = False
        self.reset()

    def _normalize_keyword(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return text
        if text in self._keyword_map:
            return self._keyword_map[text]
        low = text.lower()
        if low in self._keyword_map:
            return self._keyword_map[low]
        for kw in self.keywords:
            if kw == text or kw.lower() == low:
                return kw
        return text

    def _rebuild_stream(self):
        """重建一个干净的 stream，彻底清空历史缓冲"""
        self._stream = self._spotter.create_stream()
        logger.info("stream 已重建")

    def process(self, audio_bytes: bytes) -> Optional[str]:
        if self._paused:
            return None
        now = time.time()
        if self._debounce_sec > 0 and now - self._last_wake < self._debounce_sec:
            logger.error("防抖生效,跳过唤醒词检测")
            return None

        if isinstance(audio_bytes, np.ndarray):
            chunk = audio_bytes.astype(np.int16, copy=False)
        else:
            chunk = np.frombuffer(audio_bytes, dtype=np.int16)
        if chunk.size == 0:
            logger.error("chunk size 异常")
            return None

        # ✅ 把大 chunk 切成 20ms 小块逐步喂入
        step = int(self._sample_rate * 0.02)  # 20ms = 320 samples @ 16kHz
        for i in range(0, len(chunk), step):
            sub = chunk[i:i+step].astype(np.float32) / 32768.0
            self._stream.accept_waveform(self._sample_rate, sub)



        samples = chunk.astype(np.float32) / 32768.0

        rms = np.sqrt(np.mean(samples**2))
        logger.debug("chunk: %d samples, %.0fms, RMS=%.4f",
                     chunk.size,
                     chunk.size / self._sample_rate * 1000,
                     rms)

        # self._stream.accept_waveform(self._sample_rate, samples)

        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)

        result = self._spotter.get_result(self._stream)

        if not result or not result.strip():
            logger.debug("result 为空!")
            return None
        # self._spotter.reset_stream(self._stream)
        self._rebuild_stream()

        keyword = self._normalize_keyword(str(result))
        self._last_score = 1.0
        self._last_wake = now
        logger.info("[WakeWord] Sherpa KWS matched: %r (raw=%r)", keyword, result)

        return keyword

    def flush(self) -> Optional[str]:
        return None
