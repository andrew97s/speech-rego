语音识别客户端 — 离线部署包
Speech Recognition Client — Offline Portable Package
======================================================

技术栈: Sherpa KWS 唤醒 + FunASR fsmn-vad 判停 + 远程 Fun-ASR-Nano

快速使用 / Quick Start:
  1. 将本文件夹整体复制到目标机器
  2. 双击 check_env.bat  — 检查依赖环境
  3. 双击 start.bat      — 启动服务（控制台）
  4. 浏览器打开 http://127.0.0.1:9400/index.html
  5. 外部程序连接  ws://127.0.0.1:8766

推荐：使用 SpeechReco-Client-Setup.exe 安装为 Windows 后台服务。

端口:
  Web UI     http://127.0.0.1:9400/index.html
  WebSocket  ws://127.0.0.1:8766

系统要求:
  - Windows 10 (Build 1809+) 或 Windows 11，64-bit
  - Visual C++ 2015-2022 Redistributable (x64)
    下载: https://aka.ms/vs/17/release/vc_redist.x64.exe
  - 麦克风设备
  - 局域网内可访问的 GPU 识别服务（config.json 里 asr_remote.url）

目录说明:
  python\               Python {PY_VER} 嵌入式运行时 + 所有依赖包
  models\sherpa-kws\    唤醒词模型
  models\funasr\        fsmn-vad 缓存
  config.json           配置文件（可编辑）
  index.html            Web 控制台

构建信息:
  模式         : {WhisperModel}
  GPU 模式     : {GPU}
  构建时间     : {BuildTime}
