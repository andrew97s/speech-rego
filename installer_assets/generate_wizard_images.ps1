#Requires -Version 5.1
# Generates Inno Setup 6 wizard bitmaps (Modern UI: 164x314 left, 55x58 top-right).
param(
    [string] $OutDir = ""
)
$ErrorActionPreference = "Stop"
if (-not $OutDir) {
    $OutDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
Add-Type -AssemblyName System.Drawing

function Write-Bmp {
    param([int]$W, [int]$H, [string]$Path, [scriptblock]$Draw)
    $bmp = New-Object System.Drawing.Bitmap $W, $H
    $bmp.SetResolution(96, 96)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    & $Draw $g $W $H
    $g.Dispose()
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Bmp)
    $bmp.Dispose()
}

$accent = [System.Drawing.Color]::FromArgb(0x10, 0x78, 0xD4)
$accent2 = [System.Drawing.Color]::FromArgb(0x00, 0x5A, 0x9E)
$bg = [System.Drawing.Color]::FromArgb(0xF5, 0xF7, 0xFA)

Write-Bmp 164 314 (Join-Path $OutDir "wizard-large.bmp") {
    param($g, $W, $H)
    $rect = New-Object System.Drawing.RectangleF 0, 0, $W, $H
    $gb = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, $accent, $accent2, 45
    $g.FillRectangle($gb, $rect)
    $gb.Dispose()
    $sans = [System.Drawing.FontFamily]::GenericSansSerif
    $unit = [System.Drawing.GraphicsUnit]::Point
    $font = New-Object System.Drawing.Font($sans, [single]18.0, [System.Drawing.FontStyle]::Bold, $unit)
    $fontSub = New-Object System.Drawing.Font($sans, [single]9.0, [System.Drawing.FontStyle]::Regular, $unit)
    $white = [System.Drawing.Brushes]::White
    $g.DrawString("Speech Reco", $font, $white, 14, 24)
    $g.DrawString("Whisper · Offline", $fontSub, $white, 14, 58)
    $font.Dispose()
    $fontSub.Dispose()
}

Write-Bmp 55 58 (Join-Path $OutDir "wizard-small.bmp") {
    param($g, $W, $H)
    $rect = New-Object System.Drawing.RectangleF 0, 0, $W, $H
    $gb = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect, $accent, $accent2, 35
    $g.FillRectangle($gb, $rect)
    $gb.Dispose()
    $sans = [System.Drawing.FontFamily]::GenericSansSerif
    $font = New-Object System.Drawing.Font($sans, [single]16.0, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Point)
    $g.DrawString("SR", $font, [System.Drawing.Brushes]::White, 8, 14)
    $font.Dispose()
}

Write-Host "OK: $(Join-Path $OutDir 'wizard-large.bmp')"
Write-Host "OK: $(Join-Path $OutDir 'wizard-small.bmp')"
