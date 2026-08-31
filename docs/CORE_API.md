# 核心类与方法说明

本文档描述 `speech-rego` 项目主要 Python 模块中的核心类与公开方法。  
事件 JSON 通过 WebSocket 广播，由 `SpeechServer` 转发。

---

## 架构概览

```
WebSocket 客户端
    ↕ SpeechServer (server.py)
    ↕ SpeechEngine (engine.py)
    ├─ 唤醒: Sherpa KWS + WakeUtteranceGate
    ├─ 判停: speech_vad.FunASRVADSession（fsmn-vad）
    └─ ASR: funasr_asr 中文流式 + text_postprocess

**VAD 说明与调参**：见 [VAD.md](VAD.md)
```

**状态机**（`EngineState`）：

| 状态 | 含义 |
|------|------|
| `stopped` | 未运行 |
| `no_device` | 运行中但无麦克风，每 3s 重试 |
| `idle` | 空闲，扫描唤醒词或等待 `listen` |
| `listening` | 录音中，等待静音/超时后 ASR |

---

## wake_gating.py — `WakeUtteranceGate`

过滤「背景连续说话里误匹配关键词」的门控。默认用 RMS，可选 FunASR VAD（`gate_use_silero`）。  
详见 [VAD.md §②](VAD.md)。

| 方法 | 说明 |
|------|------|
| `__init__(maxlen=80)` | 创建滚动历史队列 |
| `reset()` | 清空块历史与噪声底 |
| `push(audio_bytes, level, *, energy_threshold, speech_vad, sample_rate, ww_cfg)` | 每块麦克风数据入队；更新 RMS / 是否像说话 |
| `may_accept_wake(ww_cfg, *, chunk_ms, energy_threshold)` | 检测器命中后复核；返回 `(bool, reason)` |

**reason 码**：`ok` | `disabled` | `no_history` | `no_speech_at_wake` | `utterance_too_long` | `warming_up` | `no_pre_silence`

---

## wake_word_match.py — 唤醒文本匹配

| 函数 | 说明 |
|------|------|
| `normalize_wake_text(text)` | 去标点/空白、小写，供比对 |
| `get_wake_word_options(ww_cfg)` | 解析 `config.wake_word` 为引擎用 dict |
| `expand_wake_phrases(keywords, prefixes, aliases)` | 展开 keyword / 前缀+keyword / 别名 |
| `match_wake_text(text, keywords, phrases, match_mode, ...)` | 统一匹配入口 |
| `match_wake_phrase_exact(...)` | `exact` 整句相等 |
| `match_wake_keyword(...)` | `substring` 子串 |
| `match_wake_after_utterance(...)` | `relaxed` |
| `to_vosk_grammar_phrase(phrase, has_cjk_fn)` | Vosk 语法表格式 |
| `build_whisper_wake_prompt(...)` | Whisper 唤醒用短 prompt |

---

## wake_detectors.py — 唤醒检测器

`SherpaKWSWakeWordDetector` 统一接口：

| 方法 | 说明 |
|------|------|
| `process(audio_bytes) -> Optional[str]` | 处理一块 PCM int16；命中返回 keyword |
| `reset()` | 重置 KeywordSpotter stream |
| `pause()` / `resume()` | 暂停/恢复（`pause_until_listen=true` 时用） |
| `flush() -> Optional[str]` | 无尾刷（Sherpa 流式实时解码） |

### `SherpaKWSWakeWordDetector`

- sherpa-onnx `KeywordSpotter` 流式 KWS
- 配置见 `wake_word.sherpa_kws`（model_dir、keywords_threshold 等）
- 未指定 `keywords_file` 时调用 `sherpa-onnx-cli text2token` 生成
- `last_score` 属性：命中时为 1.0

辅助函数：

| 函数 | 说明 |
|------|------|
| `resolve_sherpa_model_paths(cfg, base_dir)` | 解析 encoder/decoder/joiner/tokens 路径 |
| `build_sherpa_keywords_file(keywords, cfg, paths, cache_dir)` | 生成或缓存 tokenized keywords.txt |

---

## speech_vad.py — FunASR fsmn-vad（ASR 判停）

> 完整流程、配置与调参：[VAD.md §①](VAD.md)

| 符号 | 说明 |
|------|------|
| `FunASRVADSession` | 流式 fsmn-vad，默认 200ms 窗 @16kHz |
| `FunASRVADSession.reset()` | 重置 cache（每次 listen 开始/结束） |
| `FunASRVADSession.speech_fraction(pcm16_mono)` | 喂入 PCM，返回 0 或 1 |
| `FunASRVADSession.consume_speech_end()` | 上一窗是否检测到语音终点 |
| `create_funasr_vad(vad_model, sample_rate, chunk_ms)` | 工厂；失败返回 None |
| `chunk_is_speech(...)` | 占比 ≥ min_fraction 则为 speech |
| `trim_trailing_silence_chunks(...)` | 流式 VAD 下直接跳过（已等过尾静音） |

---

## funasr_asr.py — FunASR 中文流式 ASR

| 符号 | 说明 |
|------|------|
| `load_funasr_runtime(config)` | 加载 paraformer-zh-streaming + fsmn-vad（可选 ct-punc） |
| `FunASRRuntime.start_utterance()` | 新建一句的流式 session |
| `FunASRUtteranceSession.feed(pcm, is_final=False)` | 按 600ms 块 `generate`，累计文本 |
| `FunASRUtteranceSession.finish()` | `is_final=True` 冲刷尾字 |
| `FunASRRuntime.punctuate(text)` | 可选标点恢复 |

---

## text_postprocess.py — 识别后处理

| 函数 | 说明 |
|------|------|
| `get_postprocess_config(config)` | 合并 postprocess / whisper 后处理项 |
| `postprocess_transcript(text, cfg)` | 简体化 → 同音纠错 → 配置替换 → 回声过滤；空串表示丢弃 |
| `apply_configured_replacements(text, pairs)` | `replacements` 子串替换 |
| `apply_domain_keyword_corrections(text, keywords, mode)` | 词表同音/近音纠错 |
| `should_suppress_transcript(text, phrases)` | 整句仅为 TTS 回声短语时丢弃 |
| `to_simplified_chinese(text)` | 繁→简 |

---

## engine.py — `SpeechEngine`

Sherpa KWS 唤醒 + FunASR fsmn-vad 判停 + paraformer-zh-streaming；模型缓存在 stop/start 间保留。

### 公开方法

| 方法 | 说明 |
|------|------|
| `start()` / `stop()` | 同 Vosk 引擎 |
| `trigger_listen()` | 清除 `pause_until_listen`、resume 检测器、开始 LISTENING |
| `cancel_listen()` | 取消录音 |
| `suppress_input(duration_ms)` | TTS 回声屏蔽 |
| `update_config(key, value)` | 支持 `wake_word.*`、`funasr.*`、`whisper.*` 听句时长；改模型参数会 invalidate 缓存 |
| `preload()` | 预加载 FunASR + 唤醒检测器（server 启动时调用） |

### 内部要点

| 方法 | 说明 |
|------|------|
| `_ensure_models_loaded_unlocked()` | 加载 FunASR ASR/VAD、构建 wake detector |
| `_run()` | 主音频循环；IDLE 唤醒 + LISTENING 流式 ASR / fsmn-vad 判停 |
| `_finalize(...)` | 静音/超时结束；冲刷流式 ASR，发 `transcript` |
| `_postprocess_text(text)` | 调用 text_postprocess |

### 唤醒相关配置

| 键 | 说明 |
|----|------|
| `pause_until_listen` | false=可重复 wake_word；true=须 listen 后恢复扫描 |
| `wake_repeat_cooldown_ms` | 两次 wake_word 最短间隔 |
| `sherpa_kws.model_dir` | Sherpa KWS 模型目录 |
| `sherpa_kws.keywords_threshold` | 触发阈值（null 时由 sensitivity 映射） |
| `sherpa_kws.debounce_sec` | 命中后冷却时间 |

---

## server.py — `SpeechServer`

WebSocket 服务（默认端口 8765）。

| 方法 | 说明 |
|------|------|
| `__init__(config)` | 创建 `SpeechEngine` 与客户端集合 |
| `_on_engine_event(event)` | 引擎线程 → asyncio 广播 |
| `_handle_client(ws)` | 连接/断开、推送 status |
| `_dispatch(ws, raw)` | 解析 JSON 命令 |
| `_status_event()` | 当前 status JSON |
| `_tick_auto_stop_without_clients()` | 无客户端超时 stop |

### WebSocket 命令（客户端 → 服务）

| cmd | 说明 |
|-----|------|
| `start` | `engine.start()` |
| `stop` | `engine.stop()` |
| `listen` | `engine.trigger_listen()` |
| `cancel` | `engine.cancel_listen()` |
| `config` | `update_config(key, value)` |
| `suppress_input` | `suppress_input(duration_ms)` |

### 引擎事件（服务 → 客户端）

| event | 字段 |
|-------|------|
| `status` | state, wake_word_enabled, keywords, mode |
| `wake_word` | keyword, score |
| `listening_start` | trigger |
| `listening_end` | reason: silence / timeout / cancelled |
| `transcript` | text, is_final |
| `partial` | text（流式 ASR 增量） |
| `error` | code, message |

---

## server.py — `SpeechServer`（Vosk）

与 FunASR 流式版共用 `engine.py`，默认端口 8765。
