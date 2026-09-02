Windows 服务安装说明 — 语音识别客户端
====================================

1. 将整个文件夹解压到固定路径（建议勿放桌面；例如 C:\Program Files\SpeechReco-Client）。

2. 安装服务（任选其一）：
     - 双击 InstallService.bat（推荐，无需额外工具）
     - 或双击 SpeechRecoServiceSetup.exe（本地已解压目录，若包内已包含）
     - 公网分发：将本目录打成 ZIP 上传得直链后，把 SpeechRecoWebSetup.exe 发给用户；
       用户将 PackageUrl.txt.example 复制为 PackageUrl.txt 并填入该 ZIP 的 https 直链（一行），
       与 SpeechRecoWebSetup.exe 同目录，双击 exe 打开图形安装向导。
     - 或以管理员身份打开 PowerShell：cd 到本目录后执行
         Set-ExecutionPolicy -Scope Process Bypass -Force
         .\install_service.ps1

3. 卸载服务（管理员）：
     以管理员运行 PowerShell，cd 到本目录后执行 .\uninstall_service.ps1

服务说明
--------
- 服务名（短名称）: @@SERVICENAME@@
- 实际执行: cmd.exe /c start_svc.bat
- start_svc.bat 与 start.bat 基本相同（已去掉末尾 pause、title、以及 echo y| 管道，便于 Session 0 后台运行）。
- 开机自动启动；进程退出后 NSSM 约 5 秒后自动重启。
- Web UI: http://127.0.0.1:9400/index.html
- WebSocket: ws://127.0.0.1:8766
- 标准输出/错误日志: logs\service_stdout.log / logs\service_stderr.log

若 nssm status 显示 SERVICE_PAUSED（已暂停）
------------------------------------------
- 含义：Windows 认为该服务处于「暂停」而非「正在运行」。
- 在 services.msc 中请按「显示名称」查找（例如「语音识别客户端」），短名 @@SERVICENAME@@ 在列表里不一定一眼可见。
- 可尝试恢复：以管理员执行 `sc.exe continue @@SERVICENAME@@`，或在 services.msc 中对该服务右键「继续」。
- 若无效：先 `nssm stop @@SERVICENAME@@` 再 `nssm start @@SERVICENAME@@`，仍不行则卸载服务后重新执行 install_service.ps1。
- 排错请先看 logs\service_stderr.log 与 service_stdout.log。

麦克风权限
----------
部分环境需将服务登录身份改为「本地服务」或指定用户，麦克风才可正常访问。
可用 nssm edit @@SERVICENAME@@ 打开图形界面，在「Log on」中调整。

NSSM 项目: https://nssm.cc/
