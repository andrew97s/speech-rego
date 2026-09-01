# speech-rego

本地语音识别 WebSocket 服务：**Sherpa KWS** 唤醒 → **FunASR fsmn-vad** 判停 → **Fun-ASR-Nano** 句级识别（说完再出结果）。

## 快速启动

```bat
install.bat
start.bat
```

浏览器打开 `http://127.0.0.1:8080/index.html`（WebSocket 默认 `ws://127.0.0.1:8765`）。

首次启动会从 ModelScope 下载 Fun-ASR-Nano 与 fsmn-vad 到 `models/funasr/`（需联网，Nano 体积较大，建议 CUDA）。

## 架构

| 模块 | 文件 |
|------|------|
| WebSocket 服务 | `server.py` |
| 音频引擎 | `engine.py` |
| 唤醒 | `wake_detectors.py`（Sherpa-ONNX KWS） |
| 判停 VAD | `speech_vad.py`（FunASR fsmn-vad） |
| 句级 ASR | `funasr_asr.py`（Fun-ASR-Nano-2512） |
| 后处理 | `text_postprocess.py` |
| 误唤醒门控 | `wake_gating.py` |

VAD 用法与调参：[docs/VAD.md](docs/VAD.md)

LISTENING 期间只缓冲麦克风 PCM，**不实时出字**。fsmn-vad 判定说完（或静音超时）后，对整段音频调用一次 `generate`，再推送 `transcript`。

## 配置要点（`config.json`）

- `wake_word.keywords`：唤醒词（如 `小智`）；自动经 `sherpa-onnx-cli text2token` 生成 keywords 文件
- `wake_word.sherpa_kws.model_dir`：Sherpa KWS 模型目录（默认 WenetSpeech 纯中文 zipformer 3.3M）
- `funasr.asr_model`：句级 ASR，默认 `FunAudioLLM/Fun-ASR-Nano-2512`
- `funasr.vad_model`：流式 VAD，默认 `fsmn-vad`
- `funasr.device`：`cuda` 或 `cpu`
- `funasr.hub`：`ms`（ModelScope，国内默认）或 `hf`（Hugging Face）
- `funasr.language`：`中文` / `英文` / `日文`
- `whisper.max_silence_ms`：判停静音时长（同时传给 fsmn-vad `max_end_silence_time`）
- `postprocess.replacements`：识别结果词语替换
- `whisper.domain_keywords`：同时作为 Fun-ASR-Nano hotwords 与后处理纠错词表

## Sherpa KWS 模型

默认模型：[sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)（WenetSpeech L 10k 小时纯中文，`tokens_type=ppinyin`）

离线包构建时会自动下载并打包到 `models/sherpa-kws/`。

## 依赖

见 `requirements.txt`（`sherpa-onnx` + `funasr` + `torch` + `transformers`）。
