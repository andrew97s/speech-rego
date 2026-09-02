#Requires -Version 5.1
# 图形界面：下载 ZIP → 解压（逐文件进度）→ 注册服务（子进程 UTF-8 输出）
# 构建时替换 @@EMBED@@ 为 $null 或 'https://...'；@@SERVICENAME@@ 与本包 install_service.ps1 一致
$script:embeddedPackageUrl = @@EMBED@@
$script:PackagedServiceName = '@@SERVICENAME@@'

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.ServiceProcess -ErrorAction SilentlyContinue

# 以下用 .NET 替代 Remove-Item / Unblock-File / Split-Path 等，避免部分宿主（如 ps2exe）上「命名参数/参数集」绑定异常
function Remove-WebSetupFileIfExists([string]$filePath) {
    if ([string]::IsNullOrWhiteSpace($filePath)) { return }
    try {
        if ([IO.File]::Exists($filePath)) { [IO.File]::Delete($filePath) }
    } catch { }
}

function Remove-WebSetupDirectoryTree([string]$dirPath) {
    if ([string]::IsNullOrWhiteSpace($dirPath)) { return }
    $full = [IO.Path]::GetFullPath($dirPath.TrimEnd('\', '/'))
    if (-not [IO.Directory]::Exists($full)) { return }
    try {
        [IO.Directory]::Delete($full, $true)
    } catch {
        throw "删除目录失败: $full — $($_.Exception.Message)"
    }
}

function Unblock-WebSetupMotwFile([string]$filePath) {
    if ([string]::IsNullOrWhiteSpace($filePath)) { return }
    if (-not ([IO.File]::Exists($filePath))) { return }
    try {
        $ads = $filePath + ':Zone.Identifier'
        if ([IO.File]::Exists($ads)) { [IO.File]::Delete($ads) }
    } catch { }
}

function Get-LauncherDir {
    $main = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if ($main -match '(?i)\\(powershell|pwsh)\.exe$') {
        return $PSScriptRoot
    }
    return [IO.Path]::GetDirectoryName($main)
}

function Test-IsAdmin {
    $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Read-PackageUrlFile {
    $dir  = Get-LauncherDir
    $file = Join-Path $dir "PackageUrl.txt"
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    foreach ($line in (Get-Content -LiteralPath $file -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        return $t
    }
    return $null
}

function Get-InitialDownloadUrl {
    $fromFile = Read-PackageUrlFile
    if ($fromFile) { return $fromFile }
    if ($null -ne $script:embeddedPackageUrl -and "$script:embeddedPackageUrl".Trim().Length -gt 0) {
        return $script:embeddedPackageUrl.Trim()
    }
    return ""
}

function Get-InitialLocalZipHint {
    $p = $env:SPEECHRECO_LOCAL_ZIP
    if ([string]::IsNullOrWhiteSpace($p)) { return "" }
    $p = $p.Trim()
    if (Test-Path -LiteralPath $p) { return [IO.Path]::GetFullPath($p) }
    return $p
}

function Set-WebSetupProgressStyle {
    param($pb, [ValidateSet("Blocks", "Marquee")][string]$Mode)
    if ($Mode -eq "Marquee") {
        $pb.Style = [Windows.Forms.ProgressBarStyle]::Marquee
        $pb.MarqueeAnimationSpeed = 35
    } else {
        $pb.Style = [Windows.Forms.ProgressBarStyle]::Blocks
    }
    [Windows.Forms.Application]::DoEvents()
}

# 全程 UI 线程 + DoEvents；Pct=-999 不改动进度条数值（Marquee 时用）
function Update-WebSetupUi {
    param($pb, $lblSt, $tbL, [int]$Pct = -999, [string]$Status = $null, [string]$LogLine = $null)
    if ($null -ne $pb -and $Pct -ge 0) {
        if ($pb.Style -ne "Blocks") {
            $pb.Style = [Windows.Forms.ProgressBarStyle]::Blocks
        }
        $pb.Value = [Math]::Min(100, [Math]::Max(0, $Pct))
    }
    if ($null -ne $lblSt -and $null -ne $Status) { $lblSt.Text = $Status }
    if ($null -ne $tbL -and $null -ne $LogLine -and "$LogLine".Length -gt 0) {
        $t = [string]$LogLine
        # 安装失败详情可能很长；过短截断会导致弹窗/日志只剩半个乱码字符（如「S」）
        if ($t.Length -gt 24000) { $t = $t.Substring(0, 24000) + "…" }
        $tbL.AppendText("`r`n$t")
        $tbL.SelectionStart = $tbL.Text.Length
        $tbL.ScrollToCaret()
    }
    [Windows.Forms.Application]::DoEvents()
}

# 将 ErrorRecord 展开为可读文本，避免 Message 过短或乱码时用户只看到「S」
function Format-WebSetupInstallError {
    param([object]$ErrRecord)
    if ($null -eq $ErrRecord) { return "未知错误（ErrorRecord 为空）。" }
    $parts = New-Object System.Collections.Generic.List[string]
    try {
        $ex = $ErrRecord.Exception
        if ($null -eq $ex) { return [string]$ErrRecord }
        [void]$parts.Add("类型: " + $ex.GetType().FullName)
        [void]$parts.Add("消息: " + [string]$ex.Message)
        $in = $ex.InnerException
        if ($null -ne $in) {
            [void]$parts.Add("内部: " + [string]$in.Message)
        }
        if ($ex -is [System.Management.Automation.ParameterBindingException]) {
            $pbe = [System.Management.Automation.ParameterBindingException]$ex
            if ($pbe.ParameterName) { [void]$parts.Add("问题参数: " + $pbe.ParameterName) }
            if ($pbe.CommandName) { [void]$parts.Add("命令: " + $pbe.CommandName) }
        }
        if ($ex -is [System.Management.Automation.RemoteException]) {
            if ($null -ne $ErrRecord.TargetObject) {
                [void]$parts.Add("TargetObject（多为外部程序 stderr 原文）: " + [string]$ErrRecord.TargetObject)
            }
            $msgLen = if ($null -eq $ex.Message) { 0 } else { $ex.Message.Length }
            if ($msgLen -le 3) {
                [void]$parts.Add('说明: RemoteException 且消息极短时，多为 nssm.exe 等把提示写到 stderr，在 $ErrorActionPreference=Stop 下被误判为错误；安装脚本已对 nssm 丢弃 stderr。若仍失败请查看安装目录下 logs\service_stderr.log。')
            }
        }
        if ($null -ne $ErrRecord.CategoryInfo -and $ErrRecord.CategoryInfo.Reason) {
            [void]$parts.Add("类别: " + [string]$ErrRecord.CategoryInfo.Reason)
        }
        if ($null -ne $ErrRecord.FullyQualifiedErrorId -and "$($ErrRecord.FullyQualifiedErrorId)".Length -gt 0) {
            [void]$parts.Add("ErrorId: " + [string]$ErrRecord.FullyQualifiedErrorId)
        }
        if ($null -ne $ErrRecord.InvocationInfo -and $ErrRecord.InvocationInfo.PositionMessage) {
            [void]$parts.Add($ErrRecord.InvocationInfo.PositionMessage.TrimEnd())
        }
        if ($null -ne $ErrRecord.ScriptStackTrace -and "$($ErrRecord.ScriptStackTrace)".Length -gt 0) {
            [void]$parts.Add("--- ScriptStackTrace ---")
            [void]$parts.Add($ErrRecord.ScriptStackTrace.TrimEnd())
        }
        $ts = $ex.ToString()
        if ($ts.Length -gt 6000) { $ts = $ts.Substring(0, 6000) + "…" }
        [void]$parts.Add("--- Exception.ToString() ---")
        [void]$parts.Add($ts)
    } catch {
        [void]$parts.Add("格式化错误时二次异常: " + $_.Exception.Message)
    }
    return ($parts -join "`r`n")
}

function Expand-ZipWithProgress {
    param(
        [string]$ZipPath,
        [string]$DestRoot,
        $pb, $lblSt, $tbL,
        [int]$PctStart,
        [int]$PctEnd
    )
    $rootFull = [IO.Path]::GetFullPath($DestRoot.TrimEnd('\', '/'))
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = @(
            $zip.Entries | Where-Object {
                $_.FullName -and
                ($_.FullName -notmatch '[\\/]$') -and
                -not [string]::IsNullOrEmpty($_.Name)
            }
        )
        $n = $entries.Count
        if ($n -eq 0) { throw "ZIP 内没有可解压的文件。" }
        $i = 0
        foreach ($entry in $entries) {
            $i++
            $rel = $entry.FullName.Replace("/", [IO.Path]::DirectorySeparatorChar)
            $target = [IO.Path]::GetFullPath((Join-Path $rootFull $rel))
            if (-not $target.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
                throw "ZIP 内存在非法路径: $($entry.FullName)"
            }
            $dir = [IO.Path]::GetDirectoryName($target)
            if ([string]::IsNullOrWhiteSpace($dir)) { throw "ZIP 条目无法解析目录: $($entry.FullName)" }
            if (-not [IO.Directory]::Exists($dir)) {
                [void][IO.Directory]::CreateDirectory($dir)
            }
            # 杀毒/扫描可能在写入瞬间锁住 exe，短暂重试；仍失败则抛出原异常
            for ($try = 0; $try -lt 8; $try++) {
                try {
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
                    break
                } catch {
                    if ($try -eq 7) { throw $_ }
                    [Threading.Thread]::Sleep(400)
                    [Windows.Forms.Application]::DoEvents()
                }
            }
            $pct = $PctStart + [int](($PctEnd - $PctStart) * $i / [Math]::Max(1, $n))
            $st = "阶段 2/4：解压 ($i / $n)"
            Update-WebSetupUi $pb $lblSt $tbL $pct $st $null
        }
    } finally {
        $zip.Dispose()
    }
}

function Unblock-WebSetupInstallRoot {
    param([string]$RootDir)
    foreach ($name in @('nssm.exe', 'start_svc.bat', 'install_service.ps1', 'uninstall_service.ps1')) {
        $p = Join-Path $RootDir $name
        if ([IO.File]::Exists($p)) {
            Unblock-WebSetupMotwFile $p
        }
    }
}

# 覆盖安装前必须先停服务并 nssm remove，否则无法删除被占用的 nssm.exe（易被误认为「下载中途」失败）
# 不用 Get-Service/Stop-Service，避免部分宿主下「命名参数/参数集」绑定异常；改用 .NET ServiceController。
function Stop-SpeechRecoServiceForOverwrite {
    param([string]$InstallDir, [string]$ServiceName)
    if ([string]::IsNullOrWhiteSpace($ServiceName)) { return }
    if ([string]::IsNullOrWhiteSpace($InstallDir)) { return }
    $sn = $ServiceName.Trim()

    try {
        $sc = New-Object System.ServiceProcess.ServiceController $sn
        try {
            $sc.Refresh()
            if ($sc.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                $sc.Stop()
                $sc.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(45))
            }
        } finally {
            $sc.Dispose()
        }
    } catch {
        # 服务不存在或无权访问时忽略，继续尝试 nssm
    }
    Start-Sleep -Seconds 1

    $nssm = Join-Path $InstallDir "nssm.exe"
    if (-not ([IO.File]::Exists($nssm))) { return }
    Unblock-WebSetupMotwFile $nssm
    & $nssm stop $sn 2>$null
    Start-Sleep -Seconds 2
    & $nssm remove $sn confirm 2>$null
    Start-Sleep -Seconds 1
}

if (-not (Test-IsAdmin)) {
    $hostExe = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if ($hostExe -match '(?i)\\(powershell|pwsh)\.exe$') {
        $ps1 = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }
        Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File", $ps1
        )
    } else {
        Start-Process -LiteralPath $hostExe -Verb RunAs
    }
    exit 0
}

$form = New-Object Windows.Forms.Form
$form.Text = "Speech Reco 语音识别服务 — 安装程序"
$form.Size = New-Object Drawing.Size(720, 600)
$form.StartPosition = "CenterScreen"
$form.Font = New-Object Drawing.Font("Microsoft YaHei UI", 9)

$lblUrl = New-Object Windows.Forms.Label
$lblUrl.Text = "安装包下载地址 (ZIP 直链，http/https；若下方填了本地 ZIP 则忽略此项)"
$lblUrl.Location = New-Object Drawing.Point(12, 12)
$lblUrl.AutoSize = $true

$tbUrl = New-Object Windows.Forms.TextBox
$tbUrl.Location = New-Object Drawing.Point(12, 36)
$tbUrl.Size = New-Object Drawing.Size(680, 24)
$tbUrl.Text = Get-InitialDownloadUrl

$lblLocal = New-Object Windows.Forms.Label
$lblLocal.Text = "本地 ZIP（可选，测试时跳过网络下载；或设环境变量 SPEECHRECO_LOCAL_ZIP）"
$lblLocal.Location = New-Object Drawing.Point(12, 64)
$lblLocal.AutoSize = $true

$tbLocal = New-Object Windows.Forms.TextBox
$tbLocal.Location = New-Object Drawing.Point(12, 88)
$tbLocal.Size = New-Object Drawing.Size(560, 24)
$tbLocal.Text = Get-InitialLocalZipHint

$btnBrowseZip = New-Object Windows.Forms.Button
$btnBrowseZip.Text = "选 ZIP…"
$btnBrowseZip.Location = New-Object Drawing.Point(580, 84)
$btnBrowseZip.Size = New-Object Drawing.Size(110, 28)

$lblDir = New-Object Windows.Forms.Label
$lblDir.Text = "安装到目录"
$lblDir.Location = New-Object Drawing.Point(12, 120)
$lblDir.AutoSize = $true

$tbDir = New-Object Windows.Forms.TextBox
$tbDir.Location = New-Object Drawing.Point(12, 144)
$tbDir.Size = New-Object Drawing.Size(560, 24)
$tbDir.Text = if ($env:SPEECHRECO_INSTALL_ROOT) { $env:SPEECHRECO_INSTALL_ROOT.TrimEnd('\') } else { "C:\Program Files\SpeechReco-Offline" }

$btnBrowse = New-Object Windows.Forms.Button
$btnBrowse.Text = "浏览…"
$btnBrowse.Location = New-Object Drawing.Point(580, 140)
$btnBrowse.Size = New-Object Drawing.Size(110, 28)

$lblProg = New-Object Windows.Forms.Label
$lblProg.Text = "进度（1/4 下载或本地复制 → 2/4 解压 → 3/4 注册服务 → 4/4 完成）"
$lblProg.Location = New-Object Drawing.Point(12, 176)
$lblProg.AutoSize = $true

$progress = New-Object Windows.Forms.ProgressBar
$progress.Location = New-Object Drawing.Point(12, 200)
$progress.Size = New-Object Drawing.Size(680, 22)
$progress.Minimum = 0
$progress.Maximum = 100
$progress.Value = 0

$lblStatus = New-Object Windows.Forms.Label
$lblStatus.Text = "填写下载地址或本地 ZIP 后点击「开始安装」。"
$lblStatus.Location = New-Object Drawing.Point(12, 228)
$lblStatus.Size = New-Object Drawing.Size(680, 22)

$tbLog = New-Object Windows.Forms.TextBox
$tbLog.Multiline = $true
$tbLog.ScrollBars = "Vertical"
$tbLog.ReadOnly = $true
$tbLog.Location = New-Object Drawing.Point(12, 256)
$tbLog.Size = New-Object Drawing.Size(680, 200)
$tbLog.Font = New-Object Drawing.Font("Microsoft YaHei UI", 9)

$btnInstall = New-Object Windows.Forms.Button
$btnInstall.Text = "开始安装"
$btnInstall.Location = New-Object Drawing.Point(12, 472)
$btnInstall.Size = New-Object Drawing.Size(120, 32)

$btnExit = New-Object Windows.Forms.Button
$btnExit.Text = "退出"
$btnExit.Location = New-Object Drawing.Point(140, 472)
$btnExit.Size = New-Object Drawing.Size(100, 32)

$form.Controls.AddRange(@(
    $lblUrl, $tbUrl, $lblLocal, $tbLocal, $btnBrowseZip, $lblDir, $tbDir, $btnBrowse, $lblProg, $progress, $lblStatus, $tbLog, $btnInstall, $btnExit
))

$btnBrowseZip.Add_Click({
    $ofd = New-Object Windows.Forms.OpenFileDialog
    $ofd.Title = "选择本地服务安装包 ZIP"
    $ofd.Filter = "ZIP 压缩包 (*.zip)|*.zip|所有文件 (*.*)|*.*"
    if ($tbLocal.Text) {
        try {
            $ofd.InitialDirectory = [IO.Path]::GetDirectoryName($tbLocal.Text)
            $ofd.FileName = [IO.Path]::GetFileName($tbLocal.Text)
        } catch { }
    }
    if ($ofd.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
        $tbLocal.Text = $ofd.FileName
    }
})

$btnBrowse.Add_Click({
    $dlg = New-Object Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择安装目录"
    if ($tbDir.Text) { $dlg.SelectedPath = $tbDir.Text }
    if ($dlg.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
        $tbDir.Text = $dlg.SelectedPath
    }
})

$btnInstall.Add_Click({
    $u = $tbUrl.Text.Trim()
    $localZipIn = $tbLocal.Text.Trim()
    $d = $tbDir.Text.Trim().TrimEnd('\')
    $useLocalZip = $false
    $resolvedLocalZip = $null
    if ($localZipIn.Length -gt 0) {
        try {
            $resolvedLocalZip = [IO.Path]::GetFullPath($localZipIn)
        } catch {
            $resolvedLocalZip = $null
        }
        if (-not $resolvedLocalZip -or -not (Test-Path -LiteralPath $resolvedLocalZip)) {
            [Windows.Forms.MessageBox]::Show(
                "本地 ZIP 路径无效或文件不存在。",
                "提示",
                [Windows.Forms.MessageBoxButtons]::OK,
                [Windows.Forms.MessageBoxIcon]::Information
            )
            return
        }
        if ($resolvedLocalZip -notmatch '(?i)\.zip$') {
            [Windows.Forms.MessageBox]::Show(
                "本地路径须指向 .zip 文件。",
                "提示",
                [Windows.Forms.MessageBoxButtons]::OK,
                [Windows.Forms.MessageBoxIcon]::Information
            )
            return
        }
        $useLocalZip = $true
    }
    if (-not $useLocalZip -and ($u -notmatch '^https?://')) {
        [Windows.Forms.MessageBox]::Show(
            "请填写「本地 ZIP」路径，或输入以 http:// / https:// 开头的 ZIP 直链。",
            "提示",
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Information
        )
        return
    }
    if (-not $d) {
        [Windows.Forms.MessageBox]::Show(
            "请选择安装目录。",
            "提示",
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Information
        )
        return
    }
    if (-not (Test-IsAdmin)) {
        [Windows.Forms.MessageBox]::Show(
            "当前进程没有管理员权限，无法写入 Program Files 或注册系统服务。`r`n请关闭本程序后右键选择「以管理员身份运行」再安装。",
            "需要管理员权限",
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Warning
        )
        return
    }

    $tmpZip = Join-Path $env:TEMP ("SpeechRecoWeb-" + [Guid]::NewGuid().ToString("N") + ".zip")
    $tbLog.Clear()
    Set-WebSetupProgressStyle $progress "Blocks"
    $btnInstall.Enabled = $false
    $btnBrowse.Enabled = $false
    $btnBrowseZip.Enabled = $false
    $tbUrl.ReadOnly = $true
    $tbLocal.ReadOnly = $true
    $tbDir.ReadOnly = $true
    $progress.Value = 0
    Update-WebSetupUi $progress $lblStatus $tbLog 0 $(if ($useLocalZip) { "阶段 1/4：准备从本地复制…" } else { "阶段 1/4：准备下载…" }) $null

    try {
        Remove-WebSetupFileIfExists $tmpZip

        if ($useLocalZip) {
            Update-WebSetupUi $progress $lblStatus $tbLog 5 "阶段 1/4：正在从本地复制 ZIP（跳过网络）…" $null
            $srcLen = (Get-Item -LiteralPath $resolvedLocalZip).Length
            [IO.File]::Copy($resolvedLocalZip, $tmpZip, $true)
            Update-WebSetupUi $progress $lblStatus $tbLog 38 "阶段 1/4：已复制 $srcLen 字节。" $null
        } else {
            Update-WebSetupUi $progress $lblStatus $tbLog 2 "阶段 1/4：正在探测文件大小…" $null

            [int64]$total = -1
            try {
                $req = [System.Net.WebRequest]::Create($u)
                $req.Method = "HEAD"
                $req.Timeout = 15000
                $resp = $req.GetResponse()
                $cl = $resp.Headers["Content-Length"]
                if ($cl) { $total = [int64]$cl }
                $resp.Close()
            } catch { $total = -1 }

            if ($total -gt 0) {
                Update-WebSetupUi $progress $lblStatus $tbLog 3 "阶段 1/4：约 $total 字节，开始下载…" $null
            } else {
                Update-WebSetupUi $progress $lblStatus $tbLog 3 "阶段 1/4：无法获知大小，开始下载…" $null
            }

            $wc = $null
            $dlSw = [Diagnostics.Stopwatch]::StartNew()
            try {
                $wc = New-Object System.Net.WebClient
                $task = $wc.DownloadFileTaskAsync($u, $tmpZip)
                while (-not $task.IsCompleted) {
                    $got = [int64]0
                    if (Test-Path -LiteralPath $tmpZip) {
                        $got = (Get-Item -LiteralPath $tmpZip).Length
                    }
                    if ($total -gt 0) {
                        $pct = 3 + [int](34 * $got / $total)
                        if ($pct -gt 37) { $pct = 37 }
                    } else {
                        $pct = 3 + [int](34 * [Math]::Min(1.0, $dlSw.Elapsed.TotalSeconds / 120.0))
                    }
                    $msg = "阶段 1/4：已下载 {0:N0} / {1} 字节" -f $got, $(if ($total -gt 0) { $total.ToString("N0") } else { "?" })
                    Update-WebSetupUi $progress $lblStatus $tbLog $pct $msg $null
                    [Threading.Thread]::Sleep(100)
                }
                $task.GetAwaiter().GetResult()
            } finally {
                if ($null -ne $wc) { $wc.Dispose() }
            }

            if (-not (Test-Path -LiteralPath $tmpZip)) { throw "下载失败：临时文件未生成。" }
            Update-WebSetupUi $progress $lblStatus $tbLog 38 "阶段 1/4：下载完成。" $null
        }

        if (-not (Test-Path -LiteralPath $tmpZip)) { throw "阶段 1/4 失败：临时 ZIP 未生成。" }

        Update-WebSetupUi $progress $lblStatus $tbLog 39 "阶段 2/4：正在停止旧服务并清空目录…" $null
        if (Test-Path -LiteralPath $d) {
            Stop-SpeechRecoServiceForOverwrite -InstallDir $d -ServiceName ([string]$script:PackagedServiceName)
            try {
                Remove-WebSetupDirectoryTree $d
            } catch {
                throw "无法删除原安装目录（通常仍有进程占用 nssm.exe 或服务未停干净）。请先卸载服务或重启后再安装。`r`n`r`n$($_.Exception.Message)"
            }
        }
        Update-WebSetupUi $progress $lblStatus $tbLog 40 "阶段 2/4：准备解压到 $d …" $null
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($d.TrimEnd('\', '/')))

        Expand-ZipWithProgress $tmpZip $d $progress $lblStatus $tbLog 40 78
        Update-WebSetupUi $progress $lblStatus $tbLog 77 "阶段 2/4：正在解除下载安全标记（nssm 等）…" $null
        Unblock-WebSetupInstallRoot $d
        Update-WebSetupUi $progress $lblStatus $tbLog 78 "阶段 2/4：解压完成。" $null

        $marker = Join-Path $d "install_service.ps1"
        if (-not (Test-Path -LiteralPath $marker)) {
            throw "ZIP 内未找到 install_service.ps1。请确认 ZIP 为本工具生成的服务安装包（根目录即 python、models 等）。"
        }

        Update-WebSetupUi $progress $lblStatus $tbLog 80 "阶段 3/4：正在注册 Windows 服务（nssm）…" $null
        Set-WebSetupProgressStyle $progress "Marquee"

        # 子进程写入 UTF-8 日志文件（避免管道死锁 + 控制台编码乱码）；路径用环境变量传入，避免 EncodedCommand 内嵌路径引号出错
        $logAll = Join-Path $env:TEMP ("sr_inst_" + [Guid]::NewGuid().ToString("n") + ".log")
        $mEsc = $marker.Replace("'", "''")
        $inner = @"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(`$false)
`$ErrorActionPreference = 'Continue'
`$log = `$env:SR_INSTALL_LOG
if ([string]::IsNullOrWhiteSpace(`$log)) { throw 'SR_INSTALL_LOG missing (installer bug).' }
try {
  & '$mEsc' *>&1 | Out-File -FilePath `$log -Encoding UTF8
} catch {
  `$_ | Out-File -FilePath `$log -Encoding UTF8
}
if (-not (Test-Path -Path `$log)) { [IO.File]::WriteAllText(`$log, '', [Text.UTF8Encoding]::new(`$false)) }
exit (`$LASTEXITCODE)
"@
        $unicode = New-Object Text.UnicodeEncoding $false, $false
        $encB64 = [Convert]::ToBase64String($unicode.GetBytes($inner))
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "powershell.exe"
        $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encB64"
        $psi.WorkingDirectory = $d
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $false
        $psi.RedirectStandardError = $false
        $psi.CreateNoWindow = $true
        $psi.EnvironmentVariables["SR_INSTALL_LOG"] = $logAll

        $pr = [Diagnostics.Process]::Start($psi)
        $deadline = [DateTime]::UtcNow.AddMinutes(10)
        while (-not $pr.HasExited) {
            if ([DateTime]::UtcNow -gt $deadline) {
                try { $pr.Kill() } catch { }
                throw "install_service.ps1 执行超时（>10 分钟）。"
            }
            $null = $pr.WaitForExit(400)
            Update-WebSetupUi $progress $lblStatus $tbLog -999 "阶段 3/4：服务注册进行中…" $null
        }
        $code = $pr.ExitCode
        $log = ""
        if (Test-Path -LiteralPath $logAll) {
            $log = [IO.File]::ReadAllText($logAll, [Text.UTF8Encoding]::new($false)).Trim()
            Remove-WebSetupFileIfExists $logAll
        }

        Set-WebSetupProgressStyle $progress "Blocks"
        Update-WebSetupUi $progress $lblStatus $tbLog 95 "阶段 3/4：脚本已结束，退出码 $code" $log

        Update-WebSetupUi $progress $lblStatus $tbLog 100 "阶段 4/4：完成。" $null

        $logShow = $log
        if ($logShow.Length -gt 3500) { $logShow = $logShow.Substring(0, 3500) + "`r`n…(日志已截断)" }
        $okMsg = "安装流程结束。`r`n退出码: $code`r`n`r`n---- 日志 ----`r`n$logShow"
        if ($code -ne 0 -and ($logShow.Length -lt 30 -or $logShow -match '^\s*\S\s*$')) {
            $okMsg += "`r`n`r`n提示：若日志只有零星字符，请用记事本打开安装目录下 logs\service_stderr.log 与 service_stdout.log 查看完整输出。"
        }
        $lblStatus.Text = "已完成"
        if ($code -eq 0) {
            [Windows.Forms.MessageBox]::Show($okMsg, "安装完成", [Windows.Forms.MessageBoxButtons]::OK, [Windows.Forms.MessageBoxIcon]::Information)
        } else {
            [Windows.Forms.MessageBox]::Show($okMsg, "安装结束（有警告）", [Windows.Forms.MessageBoxButtons]::OK, [Windows.Forms.MessageBoxIcon]::Warning)
        }
    } catch {
        Set-WebSetupProgressStyle $progress "Blocks"
        $lblStatus.Text = "失败"
        $detail = Format-WebSetupInstallError $_
        Update-WebSetupUi $progress $lblStatus $tbLog 0 "失败" $detail
        $box = "发生错误（完整信息已写入上方日志区，可复制）：`r`n`r`n" + $detail
        if ($detail -match 'nssm|访问被拒绝|Access is denied|ParameterBinding|RemoteException') {
            $box += "`r`n`r`n建议：以管理员运行本程序；RemoteException 常为外部程序 stderr 被误判；若含 ParameterBinding，多为脚本/宿主兼容问题，请把日志区全文发开发者。安全软件可能拦截 nssm.exe。"
        }
        $show = $box
        if ($show.Length -gt 4000) { $show = $show.Substring(0, 4000) + "`r`n…(弹窗已截断，见上方日志区)" }
        [Windows.Forms.MessageBox]::Show(
            $show,
            "安装失败",
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Error
        )
    } finally {
        Set-WebSetupProgressStyle $progress "Blocks"
        Remove-WebSetupFileIfExists $tmpZip
        $btnInstall.Enabled = $true
        $btnBrowse.Enabled = $true
        $btnBrowseZip.Enabled = $true
        $tbUrl.ReadOnly = $false
        $tbLocal.ReadOnly = $false
        $tbDir.ReadOnly = $false
        [Windows.Forms.Application]::DoEvents()
    }
})

$btnExit.Add_Click({ $form.Close() })

[Windows.Forms.Application]::EnableVisualStyles()
[void][Windows.Forms.Application]::Run($form)
