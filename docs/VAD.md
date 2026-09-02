# VAD 在本项目中的用法

本项目里和「有没有人在说话」相关的逻辑分 **两层**，不要混为一谈：

| 层级 | 模块 | 何时运行 | 作用 |
|------|------|----------|------|
| **① ASR 判停** | `speech_vad.FunASRVADSession` | `LISTENING` 录音 | 用户**说完一句**后结束录音，再提交给远程 Fun-ASR-Nano |
| **② 唤醒门控** | `wake_gating.WakeUtteranceGate` | `IDLE` 唤醒扫描 | 过滤误唤醒（默认用 **RMS**，可选 FunASR VAD） |

唤醒检测本身由 **Sherpa KWS**（`wake_detectors.SherpaKWSWakeWordDetector`）完成，不依赖 VAD。

**Fun-ASR-Nano 本身不负责切句**。判停在 Windows 客户端完成；LISTENING 期间只累积 PCM，判停后 POST 整段给 GPU 识别服务。

---

## 整体数据流

```
麦克风 chunk (16kHz int16)
        │
        ├─ IDLE ─────────────────────────────────────────────┐
        │     Sherpa KWS KeywordSpotter                       │
        │     WakeUtteranceGate.push() ← ② 可选 FunASR VAD/RMS │
        │     may_accept_wake() → wake_word 事件              │
        │                                                      │
        └─ LISTENING ────────────────────────────────────────┤
              listen_buf 累积 PCM                             │
              FunASRVADSession（fsmn-vad）                     │
                [[beg,-1]] 语音开始 / [[-1,end]] 语音结束       │
              结束或连续静音 → _finalize()                      │
                    └ POST asr_server /v1/recognize → transcript │
```

---

## ① ASR 判停（主路径，必装 funasr）

### 实现

- 文件：`speech_vad.py` → 类 `FunASRVADSession`
- 依赖：`funasr`（模型 `fsmn-vad`）
- 引擎：`engine.py` preload 加载 VAD 模型，打开麦克风流时 `create_funasr_vad()`

### 算法（按 chunk）

1. 每个麦克风块（默认约 250ms，`audio.chunk_size=4000`）在 LISTENING 态追加到 `listen_buf`（不调用 ASR）。
2. 同一块 PCM 进入 fsmn-vad（内部按 `funasr.vad_chunk_ms`，默认 200ms 切窗）。
3. 流式 VAD 输出（时间单位 ms）：
   - `[[beg, -1]]`：检测到语音起点 → `in_speech=True`
   - `[[-1, end]]` 或 `[[beg, end]]`：检测到终点 → 判停（`reason=silence`）
   - `[]`：无事件
4. 若 VAD 未给出终点，则回退到连续「非 speech」块达到 `max_silence_ms` 后结束。

加载 VAD 时会把 `whisper.max_silence_ms` 传给 fsmn-vad 的 `max_end_silence_time`。

### 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `funasr.vad_model` | `fsmn-vad` | FunASR VAD 模型名 |
| `funasr.vad_chunk_ms` | 200 | 流式 VAD 窗长（ms） |
| `whisper.max_silence_ms` | 1500 | 连续静音超过此值触发结束；同时作为 `max_end_silence_time` |
| `whisper.min_listen_ms` | 1000 | 最短录音时长 |
| `whisper.max_listen_ms` | 30000 | 最长录音时长 |

---

## ② 唤醒门控（WakeUtteranceGate）

Sherpa KWS 命中后，**不一定**立刻发 `wake_word`。Gate 检查：

1. 唤醒前是否有足够静音（`require_pre_silence_ms`）
2. 唤醒段 RMS 是否像真实说话（`speech_energy_ratio`）
3. 唤醒段是否过长（`max_wake_utterance_ms`）

- **默认** `wake_word.gate_use_silero: false` → Gate 只看 RMS + `energy_threshold`，**不调用** `_speech_vad`。
- `gate_use_silero: true` 时，Gate 内额外用 FunASR VAD 块状态过滤（配置键名沿用历史）。

### 配置（`config.json` → `wake_word`）

| 键 | 默认 | 说明 |
|----|------|------|
| `require_pre_silence_ms` | 350 | 唤醒前需静音时长 |
| `pre_silence_min_ratio` | 0.65 | 静音窗占比 |
| `pre_silence_level_ratio` | 0.32 | 安静 RMS 相对 `energy_threshold` 的比例 |
| `gate_use_silero` | false | 是否在 Gate 里叠加 FunASR VAD |
| `gate_silero_fraction` | 0.28 | Gate 用 VAD 时的块 speech 占比阈值 |

拒绝原因码见 `wake_gating.may_accept_wake()`：`no_pre_silence`、`utterance_too_long` 等。

---

## 代码索引

| 功能 | 位置 |
|------|------|
| FunASR VAD 会话与工具函数 | `speech_vad.py` |
| 句级 ASR 封装 | `funasr_asr.py` |
| 创建 `_speech_vad`、LISTENING 判停循环 | `engine.py` → `_run()` |
| 判停条件 | `engine.py` → `_still_speaking_for_end()` / `consume_speech_end()` |
| Gate 可选 VAD | `wake_gating.py` → `push()` |
| Gate / Sherpa 配置解析 | `wake_config.py` |
| Sherpa KWS 检测器 | `wake_detectors.py` → `SherpaKWSWakeWordDetector` |

---

## 与 FunASR 离线 VAD 切分的区别

FunASR 非流式用法可以把 `vad_model="fsmn-vad"` 绑在 ASR `AutoModel` 上，对整段 wav 先切句再转写。本项目是 **实时麦克风 + WebSocket 事件**，因此：

- Windows 客户端按 chunk 喂 VAD 做判停，PCM 只进 `listen_buf`
- 句尾把整段 POST 到 GPU 上的 Fun-ASR-Nano，不推送 `partial`
