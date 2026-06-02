# 核心类与方法说明

本文档描述 `speech-rego` 项目主要 Python 模块中的核心类与公开方法。  
事件 JSON 通过 WebSocket 广播，由 `SpeechServer` 转发。

---

## 架构概览

```
WebSocket 客户端
    ↕ SpeechServer (server_whisper.py / server.py)
    ↕ SpeechEngine (engine_whisper.py / engine.py)
    ├─ 唤醒: WakeUtteranceGate + wake_detectors.*
    ├─ 判停: speech_vad.SileroVADSession (Whisper 路径)
    └─ 后处理: text_postprocess.postprocess_transcript
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

过滤「背景连续说话里误匹配关键词」的门控。

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

三种检测器统一接口：

| 方法 | 说明 |
|------|------|
| `process(audio_bytes) -> Optional[str]` | 处理一块 PCM；命中返回 keyword |
| `reset()` | 重置内部状态 |
| `pause()` / `resume()` | 暂停/恢复（`pause_until_listen=true` 时用） |
| `flush() -> Optional[str]` | 刷出尾部（Vosk 专用） |

### `OpenWakeWordWakeWordDetector`

- openWakeWord ONNX/tflite 分数型唤醒
- 缓冲至 1280 样本再 `predict`
- `last_score` 属性：最近一次命中分数

### `VoskWakeWordDetector`

- Vosk 语法表 + 整句/ partial 匹配
- 依赖 `wake_word_match.match_wake_text`

### `WhisperWakeWordDetector`

- 2.5s 滑窗 + faster-whisper 转写 + 文本匹配
- 无 VAD，CPU 开销较大

---

## speech_vad.py — Silero VAD（ASR 判停）

| 符号 | 说明 |
|------|------|
| `SileroVADSession` | 流式 Silero ONNX，512 样本/窗 @16kHz |
| `SileroVADSession.reset()` | 重置 RNN 状态（每次 listen 开始/结束） |
| `SileroVADSession.speech_fraction(pcm16_mono)` | 块内语音窗占比 0~1 |
| `create_silero_vad(threshold, sample_rate)` | 工厂；失败返回 None |
| `chunk_is_speech(...)` | 占比 ≥ min_fraction 则为 speech |
| `trim_trailing_silence_chunks(buf, vad, ...)` | Whisper 推理前裁尾静音 |
| `silero_threshold_from_config(w)` | 读 `whisper.silero_threshold` |
| `silero_speech_fraction_from_config(w)` | 读 `whisper.silero_speech_fraction` |

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

## engine.py — `SpeechEngine`（Vosk ASR）

Vosk 流式识别 + 唤醒（openWakeWord / Vosk）。

### 公开方法

| 方法 | 说明 |
|------|------|
| `__init__(config, event_callback)` | `event_callback(dict)` 发事件 |
| `start()` | 后台线程启动麦克风循环 |
| `stop()` | 停止并释放麦克风 |
| `trigger_listen()` | 进入 LISTENING（手动或唤醒后） |
| `cancel_listen()` | 取消当前识别 |
| `suppress_input(duration_ms)` | 忽略麦克风 N ms（TTS 回声） |
| `update_config(key, value)` | 运行时改 config 点路径 |

### 内部要点

| 方法 | 说明 |
|------|------|
| `_run()` | 主循环：加载 Vosk、建唤醒器、sounddevice 回调 |
| `_set_state(state)` | 状态变更 + 发 `status` 事件 |
| `_finalize(rec, reason)` | Vosk 一句结束，发 `transcript` / `listening_end` |

---

## engine_whisper.py — `SpeechEngine`（Whisper ASR）

Whisper 批量识别 + 多模式唤醒；模型缓存在 stop/start 间保留。

### 公开方法

| 方法 | 说明 |
|------|------|
| `start()` / `stop()` | 同 Vosk 引擎 |
| `trigger_listen()` | 清除 `pause_until_listen`、resume 检测器、开始 LISTENING |
| `cancel_listen()` | 取消录音 |
| `suppress_input(duration_ms)` | TTS 回声屏蔽 |
| `update_config(key, value)` | 支持 `wake_word.*`、`whisper.*`；改模型参数会 invalidate 缓存 |
| `preload()` | 预加载 Whisper + 唤醒检测器（server 启动时调用） |

### 内部要点

| 方法 | 说明 |
|------|------|
| `_ensure_models_loaded_unlocked()` | 加载 faster-whisper、构建 wake detector |
| `_run()` | 主音频循环；IDLE 唤醒 + LISTENING Silero 判停 |
| `_do_transcribe(model, buf, language)` | Whisper 推理 + 幻听/续写过滤 |
| `_finalize(...)` | 静音/超时结束；发 `transcript` |
| `_transcribe_options()` | 从 config 组装 Whisper kwargs |
| `_postprocess_text(text)` | 调用 text_postprocess |

### 唤醒相关配置

| 键 | 说明 |
|----|------|
| `pause_until_listen` | false=可重复 wake_word；true=须 listen 后恢复扫描 |
| `wake_repeat_cooldown_ms` | 两次 wake_word 最短间隔 |
| `oww_vad_threshold` | 建议 0；>0 易触发 ONNX 形状错误 |

---

## server_whisper.py — `SpeechServer`

Whisper 版 WebSocket 服务（默认端口 8766）。

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
| `partial` | text（Whisper 可选） |
| `error` | code, message |

---

## server.py — `SpeechServer`（Vosk）

与 Whisper 版类似，使用 `engine.py`，默认端口 8765。
