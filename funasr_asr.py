"""
Fun-ASR-Nano 句级 ASR 封装。

LISTENING 期间只缓冲 PCM；VAD 判停后对整段音频调用一次 generate。
模型通过 ModelScope（默认 hub=ms）下载，缓存目录见 config funasr.cache_dir。
"""
from __future__ import annotations

import logging
import os
import tempfile
import wave
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_DEFAULT_ASR = "FunAudioLLM/Fun-ASR-Nano-2512"

_LANG_MAP = {
    "zh": "中文",
    "cn": "中文",
    "chinese": "中文",
    "en": "英文",
    "english": "英文",
    "ja": "日文",
    "jp": "日文",
    "japanese": "日文",
}


def resolve_funasr_device(device: str) -> str:
    """把 config 里的 cuda / cpu / dml 映射成 FunASR AutoModel 的 device 字符串。"""
    d = (device or "cpu").strip().lower()
    if d in ("cuda", "gpu"):
        d = "cuda:0"
    if d.startswith("cuda"):
        try:
            import torch

            if torch.cuda.is_available():
                return d
        except Exception:
            pass
        logger.warning("[FunASR] CUDA requested but unavailable; using cpu")
        return "cpu"
    if d in ("dml", "directml", "mps"):
        if d == "mps":
            return "mps"
        logger.warning("[FunASR] device=%r is not supported; using cpu", device)
        return "cpu"
    return "cpu"


def apply_modelscope_cache(cache_dir: Optional[str], base_dir: Optional[Path] = None) -> Path:
    """设置 MODELSCOPE_CACHE / HF 缓存，使模型落到项目 models/funasr（或配置路径）。"""
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    raw = (cache_dir or "models/funasr").strip() or "models/funasr"
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MODELSCOPE_CACHE", str(path))
    os.environ.setdefault("MODELSCOPE_MODULES_CACHE", str(path))
    hf_home = path / "hf"
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(hf_home))
    return path


def _pcm16_to_f32(pcm16_mono: bytes) -> np.ndarray:
    if not pcm16_mono:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm16_mono, dtype=np.int16).astype(np.float32) / 32768.0


def _extract_text(res: Any) -> str:
    if not res:
        return ""
    item = res[0] if isinstance(res, (list, tuple)) else res
    if isinstance(item, dict):
        return str(item.get("text") or "").strip()
    return str(item).strip()


def _extract_vad_value(res: Any) -> list:
    if not res:
        return []
    item = res[0] if isinstance(res, (list, tuple)) else res
    if isinstance(item, dict):
        val = item.get("value")
        return val if isinstance(val, list) else []
    return []


def funasr_cfg(config: dict) -> dict:
    return dict(config.get("funasr") or {})


def hotword_list_from_config(config: dict) -> List[str]:
    """Fun-ASR-Nano 的 hotwords 是字符串列表。"""
    f = funasr_cfg(config)
    explicit = f.get("hotword")
    parts: List[str] = []
    if explicit and str(explicit).strip():
        raw = str(explicit).strip()
        if "," in raw:
            parts.extend(p.strip() for p in raw.split(",") if p.strip())
        else:
            parts.extend(p.strip() for p in raw.split() if p.strip())
        return parts
    w = config.get("whisper") or {}
    kws = w.get("domain_keywords")
    if isinstance(kws, list):
        parts.extend(str(k).strip() for k in kws if k and str(k).strip())
    elif isinstance(kws, str) and kws.strip():
        parts.append(kws.strip())
    return parts


def asr_language_from_config(config: dict) -> str:
    f = funasr_cfg(config)
    explicit = f.get("language")
    if explicit and str(explicit).strip():
        key = str(explicit).strip()
        return _LANG_MAP.get(key.lower(), key)
    w = (config.get("whisper") or {}).get("language") or "zh"
    return _LANG_MAP.get(str(w).strip().lower(), "中文")


class FunASRRuntime:
    """已加载的 Fun-ASR-Nano / fsmn-vad（可选 ct-punc），以及整句转写。"""

    def __init__(
        self,
        asr_model: Any,
        vad_model: Any,
        punc_model: Any,
        vad_chunk_ms: int,
        hotwords: Optional[List[str]] = None,
        device: str = "cpu",
        asr_name: str = "",
        vad_name: str = "",
        language: str = "中文",
        itn: bool = True,
    ):
        self.asr_model = asr_model
        self.vad_model = vad_model
        self.punc_model = punc_model
        self.vad_chunk_ms = int(vad_chunk_ms)
        self.hotwords = list(hotwords or [])
        self.device = device
        self.asr_name = asr_name
        self.vad_name = vad_name
        self.language = language
        self.itn = bool(itn)

    def punctuate(self, text: str) -> str:
        if not text or self.punc_model is None:
            return text
        try:
            out = _extract_text(self.punc_model.generate(input=text))
            return out or text
        except Exception as exc:
            logger.debug("[FunASR] punc failed: %s", exc)
            return text

    def transcribe_buffer(self, pcm_chunks: List[bytes]) -> str:
        if not pcm_chunks or self.asr_model is None:
            return ""
        return self.transcribe_pcm(b"".join(pcm_chunks))

    def transcribe_pcm(self, pcm16_mono: bytes) -> str:
        """对一整段 16 kHz int16 PCM 做一次 Fun-ASR-Nano generate。"""
        if not pcm16_mono or self.asr_model is None:
            return ""
        kw = self._generate_kwargs()
        audio = np.ascontiguousarray(_pcm16_to_f32(pcm16_mono), dtype=np.float32)
        try:
            text = self._call_generate(audio, kw)
        except Exception as exc:
            logger.debug(
                "[FunASR] ndarray generate failed (%s); trying wav",
                type(exc).__name__,
            )
            text = self._generate_via_wav(pcm16_mono, kw)
        if not text:
            return ""
        return self.punctuate(text)

    def _generate_kwargs(self) -> dict:
        kw: dict = {
            "cache": {},
            "batch_size": 1,
            "language": self.language,
            "itn": self.itn,
        }
        if self.hotwords:
            kw["hotwords"] = list(self.hotwords)
        return kw

    def _call_generate(self, audio_or_path: Any, kw: dict) -> str:
        """调用 AutoModel.generate；热词不被接受时去掉后重试。失败则抛出。"""
        call = dict(kw)
        try:
            return _extract_text(self.asr_model.generate(input=audio_or_path, **call))
        except Exception as exc:
            if "hotwords" not in call and "hotword" not in call:
                raise
            call.pop("hotwords", None)
            call.pop("hotword", None)
            self.hotwords = []
            logger.warning(
                "[FunASR] generate with hotwords failed (%s); retry without",
                type(exc).__name__,
            )
            return _extract_text(self.asr_model.generate(input=audio_or_path, **call))

    def _generate_via_wav(self, pcm16_mono: bytes, kw: dict) -> str:
        path = ""
        try:
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="funasr-nano-")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(_SAMPLE_RATE)
                wf.writeframes(pcm16_mono)
            try:
                return self._call_generate(path, kw)
            except Exception:
                return self._call_generate([path], kw)
        except Exception as exc:
            logger.warning("[FunASR] wav generate failed: %s", exc)
            return ""
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _is_funasr_nano(asr_name: str) -> bool:
    n = (asr_name or "").lower()
    return "fun-asr-nano" in n or "funasr-nano" in n or "funasr_nano" in n


def _ensure_funasr_nano_registered() -> None:
    """
    funasr>=1.4 已内置 FunASRNano。官方文档里的 remote_code=./model.py
    会从进程 CWD 找文件；ModelScope 权重目录里没有这个文件，会报
    No module named 'model'。先 import 内置类完成注册即可。
    """
    try:
        from funasr.models.fun_asr_nano.model import FunASRNano  # noqa: F401
    except Exception as exc:
        logger.error(
            "[FunASR] cannot import FunASRNano (%s): %s. "
            "On the GPU server run: pip install tiktoken huggingface_hub transformers",
            type(exc).__name__,
            exc,
        )
        raise
    logger.info("[FunASR] FunASRNano registered from funasr.models.fun_asr_nano")


def _remote_code_kwargs(f: dict, base_dir: Path) -> dict:
    """
    仅当 remote_code 指向真实存在的 .py 时才打开 trust_remote_code。
    默认不再传 ./model.py（官方示例假定在 Fun-ASR 仓库根目录运行）。
    """
    raw = str(f.get("remote_code") or "").strip()
    if raw in ("model", "./model.py", "model.py"):
        raw = ""
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if path.is_file():
            return {
                "trust_remote_code": True,
                "remote_code": str(path),
            }
        logger.warning("[FunASR] remote_code file not found: %s; using built-in Nano class", path)
    if bool(f.get("trust_remote_code", False)) and raw:
        return {"trust_remote_code": True, "remote_code": raw}
    return {}


def load_funasr_runtime(
    config: dict,
    base_dir: Optional[Path] = None,
    components: Optional[Sequence[str]] = None,
) -> FunASRRuntime:
    """
    按需加载 Fun-ASR-Nano / fsmn-vad / ct-punc。

    components 默认 asr+vad；Windows 客户端只加载 vad，GPU 识别服务只加载 asr。
    """
    from funasr import AutoModel

    want = {str(c).strip().lower() for c in (components or ("asr", "vad")) if c}
    if not want:
        want = {"asr", "vad"}

    f = funasr_cfg(config)
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    cache = apply_modelscope_cache(f.get("cache_dir"), base_dir=root)
    device = resolve_funasr_device(str(f.get("device") or "cpu"))
    ncpu = int(f.get("ncpu", 4))
    disable_update = bool(f.get("disable_update", True))

    asr_name = str(f.get("asr_model") or _DEFAULT_ASR).strip() or _DEFAULT_ASR
    vad_name = str(f.get("vad_model") or "fsmn-vad").strip()
    punc_name = str(f.get("punc_model") or "").strip()
    language = asr_language_from_config(config)
    itn = bool(f.get("itn", True))

    common = dict(
        device=device,
        ncpu=ncpu,
        disable_pbar=True,
        disable_update=disable_update,
    )
    hub = f.get("hub")
    if hub:
        common["hub"] = str(hub)
    else:
        common["hub"] = "ms"

    asr_kw = dict(common)
    asr_kw.update(_remote_code_kwargs(f, root))

    logger.info(
        "[FunASR] loading components=%s asr=%s vad=%s punc=%s device=%s hub=%s cache=%s",
        ",".join(sorted(want)),
        asr_name if "asr" in want else "(skip)",
        vad_name if "vad" in want else "(skip)",
        punc_name or "(off)",
        device,
        asr_kw.get("hub"),
        cache,
    )
    logging.getLogger("funasr").setLevel(logging.WARNING)
    logging.getLogger("modelscope").setLevel(logging.WARNING)

    asr_model = None
    if "asr" in want:
        if _is_funasr_nano(asr_name):
            _ensure_funasr_nano_registered()
        asr_model = AutoModel(model=asr_name, **asr_kw)

    vad_model = None
    if "vad" in want:
        vad_kw = dict(common)
        max_end = f.get("max_end_silence_ms")
        if max_end is None:
            max_end = (config.get("whisper") or {}).get("max_silence_ms")
        if max_end is not None:
            vad_kw["max_end_silence_time"] = int(max_end)
        vad_model = AutoModel(model=vad_name, **vad_kw)

    punc_model = None
    if "punc" in want and punc_name:
        try:
            punc_model = AutoModel(model=punc_name, **common)
        except Exception as exc:
            logger.warning("[FunASR] punctuation model %r failed: %s", punc_name, exc)

    logger.info("[FunASR] models ready")
    return FunASRRuntime(
        asr_model=asr_model,
        vad_model=vad_model,
        punc_model=punc_model,
        vad_chunk_ms=int(f.get("vad_chunk_ms", 200)),
        hotwords=hotword_list_from_config(config),
        device=device,
        asr_name=asr_name if "asr" in want else "(remote)",
        vad_name=vad_name if "vad" in want else "(off)",
        language=language,
        itn=itn,
    )


def funasr_model_key(config: dict, components: Optional[Sequence[str]] = None) -> Tuple:
    """缓存失效用的配置指纹。"""
    f = funasr_cfg(config)
    r = dict(config.get("asr_remote") or {})
    want = tuple(sorted(str(c).strip().lower() for c in (components or ("asr", "vad")) if c))
    return (
        want,
        str(f.get("asr_model") or _DEFAULT_ASR),
        str(f.get("vad_model") or "fsmn-vad"),
        str(f.get("punc_model") or ""),
        resolve_funasr_device(str(f.get("device") or "cpu")),
        int(f.get("ncpu", 4)),
        int(f.get("vad_chunk_ms", 200)),
        str(f.get("cache_dir") or "models/funasr"),
        str(f.get("hub") or "ms"),
        asr_language_from_config(config),
        bool(f.get("itn", True)),
        tuple(hotword_list_from_config(config)),
        bool(r.get("enabled", False)),
        str(r.get("url") or ""),
    )
