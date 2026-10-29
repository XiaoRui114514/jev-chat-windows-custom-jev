# 给 jev-chat-windows.exe 打「判断模型也能选自定义来源」的补丁。
# 只做三件事：找到 exe、核对 sha256、把 patch/ 里那几段字节拼回去。全程离线，不装任何东西。
param(
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Path,
    [switch]$Restore,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$here = $PSScriptRoot
$manifest = Get-Content (Join-Path $here 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json

function Say($text) { Write-Host $text }
function Fail($text) { Write-Host ''; Write-Host "× $text" -ForegroundColor Red; Write-Host ''; exit 1 }

# 自己算 sha256：Windows PowerShell 5.1 的 Get-FileHash 依赖模块自动加载，环境被弄乱时会找不到
function File-Hash($file) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($file)
    try { -join ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Bytes-Hash([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { -join ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Resolve-Exe($candidate) {
    if (-not $candidate) { return $null }
    $candidate = $candidate.Trim('"').Trim()
    if (-not (Test-Path -LiteralPath $candidate)) { return $null }
    $item = Get-Item -LiteralPath $candidate
    if ($item.PSIsContainer) {
        $inner = Join-Path $item.FullName 'jev-chat-windows.exe'
        if (Test-Path -LiteralPath $inner) { return (Get-Item -LiteralPath $inner).FullName }
        return $null
    }
    $item.FullName
}

function Find-Candidates {
    $found = New-Object System.Collections.Generic.List[string]
    foreach ($p in $Path) { $r = Resolve-Exe $p; if ($r) { $found.Add($r) } }
    # An explicit target must not fall through to an unrelated installation.
    if ($Path.Count -gt 0) {
        $found | Select-Object -Unique
        return
    }
    # bat 就放在 app 目录里
    $here_exe = Join-Path (Split-Path -Parent $here) 'jev-chat-windows.exe'
    if (Test-Path -LiteralPath $here_exe) { $found.Add((Get-Item -LiteralPath $here_exe).FullName) }
    # 桌面上的快捷方式（JevChat 微信助手.lnk 之类）
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ($desktop -and (Test-Path -LiteralPath $desktop)) {
        try {
            $shell = New-Object -ComObject WScript.Shell
            foreach ($lnk in Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -ErrorAction SilentlyContinue) {
                try {
                    $target = $shell.CreateShortcut($lnk.FullName).TargetPath
                    if ($target -and (Split-Path -Leaf $target) -ieq 'jev-chat-windows.exe' -and
                        (Test-Path -LiteralPath $target)) { $found.Add($target) }
                } catch { }
            }
        } catch { }
    }
    # 常见下载/解压位置，深度限制住，别满盘扫
    $roots = @($desktop, (Join-Path $env:USERPROFILE 'Downloads'),
               (Join-Path $env:USERPROFILE 'Documents'),
               (Join-Path $env:USERPROFILE 'Documents\Codex'))
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -Recurse -Depth 4 -Filter 'jev-chat-windows.exe' -File -ErrorAction SilentlyContinue |
            ForEach-Object { $found.Add($_.FullName) }
    }
    $seen = @{}
    foreach ($f in $found) { if (-not $seen.ContainsKey($f)) { $seen[$f] = $true; $f } }
}

function Stop-App($exe) {
    $procs = @(Get-Process -Name 'jev-chat-windows' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -ieq $exe) })
    if ($procs.Count -gt 0) {
        Say '程序正在运行，先关掉它。'
        foreach ($p in $procs) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
}

$version = $manifest.app_version -replace '^v', ''
Say ''
Say "JevChat-Windows 判断模型自定义来源补丁  (对应 v$version)"
Say ''

# ---- 找 exe：只认 sha256 对得上的那个 ----
if ($Check) {
    $reports = @()
    foreach ($candidate in Find-Candidates) {
        $hash = File-Hash $candidate
        $backup = "$candidate.bak"
        $state = if ($hash -eq $manifest.original.sha256) { 'original' } elseif ($hash -eq $manifest.patched.sha256) { 'patched' } else { 'unsupported' }
        $reports += [pscustomobject]@{ path=$candidate; version=$version; state=$state; sha256=$hash; restorable=((Test-Path -LiteralPath $backup) -and ((File-Hash $backup) -eq $manifest.original.sha256)) }
    }
    ConvertTo-Json -InputObject @($reports) -Depth 4
    if ($reports.Count -eq 0) { exit 2 }
    exit 0
}

$target = $null
$stale = @()
foreach ($candidate in Find-Candidates) {
    $hash = File-Hash $candidate
    $backup = "$candidate.bak"
    $backupOk = (Test-Path -LiteralPath $backup) -and ((File-Hash $backup) -eq $manifest.original.sha256)
    if ($Restore) {
        if ($backupOk) { $target = $candidate; break }
        Say "（跳过 $candidate：没有通过校验的原始 exe 备份）"
        continue
    }
    if ($hash -eq $manifest.original.sha256 -or $hash -eq $manifest.patched.sha256) {
        $target = $candidate
        break
    }
    # 已经改过的：这一版补丁、或者别的版本补丁，只要备份还在就算「改过」
    if ($hash -eq $manifest.patched.sha256 -or $backupOk) { $stale += $candidate; continue }
    Say "（跳过 $candidate：不是 v$version 的官方 exe）"
}
if (-not $target) {
    if ($Restore) {
        Fail '没找到通过校验的原始 exe 备份，未修改任何文件。请检查指定 exe 旁的 .bak 文件。'
    } elseif ($stale.Count -gt 0) {
        Fail "这个 exe 已经打过补丁或改动过：`n  $($stale[0])`n先双击 卸载补丁.bat 还原成官方版，再来打补丁。"
    } else {
        Fail "没找到 v$version 的官方 jev-chat-windows.exe。把 exe（或它所在的文件夹）拖到 安装补丁.bat 上再跑一次。"
    }
}

$backup = "$target.bak"
Say "目标：$target"

# ---- 还原 ----
if ($Restore) {
    if (-not (Test-Path -LiteralPath $backup)) { Fail "没找到备份 $backup，没法还原。" }
    if ((File-Hash $backup) -ne $manifest.original.sha256) {
        Fail '备份文件校验不对，未修改原来的 exe。'
    }
    Stop-App $target
    Copy-Item -LiteralPath $backup -Destination $target -Force
    $back = (File-Hash $target)
    if ($back -ne $manifest.original.sha256) { Fail "还原后的 exe 校验不对（$back），备份文件可能也被改过。" }
    Say '已还原成打补丁前的 exe。'
    exit 0
}

# ---- 打补丁 ----
if ((File-Hash $target) -eq $manifest.patched.sha256) { Say '这个 exe 已经打过补丁了，不用再来一次。'; exit 0 }
if ((Test-Path -LiteralPath $backup) -and ((File-Hash $backup) -ne $manifest.original.sha256)) {
    Fail "现有备份 $backup 校验不对，未修改原来的 exe。请先保留并检查这份备份。"
}
$payloads = @{}
foreach ($name in @($manifest.main_entry.file, $manifest.pyz.header_file, $manifest.pyz.append_file,
                    $manifest.pyz.toc_file, $manifest.carchive.toc_file, $manifest.carchive.cookie_file)) {
    $payloads[$name] = [IO.File]::ReadAllBytes((Join-Path $here $name))
}
if ($manifest.payload_sha256) {
    foreach ($prop in $manifest.payload_sha256.PSObject.Properties) {
        if (-not $payloads.ContainsKey($prop.Name) -or (Bytes-Hash $payloads[$prop.Name]) -ne $prop.Value) { Fail "补丁载荷校验失败：$($prop.Name)，未修改程序。" }
    }
}
Stop-App $target
$original = [IO.File]::ReadAllBytes($target)
$archiveStart = [int]$manifest.archive_start
$mainOffset = $archiveStart + [int]$manifest.main_entry.offset
$mainLength = [int]$manifest.main_entry.length
$pyzOffset = $archiveStart + [int]$manifest.pyz.offset
$pyzHeaderLength = [int]$manifest.pyz.header_length
$pyzTocOffset = [int]$manifest.pyz.toc_offset

$ms = New-Object System.IO.MemoryStream
$ms.Write($original, 0, $mainOffset)
$ms.Write($payloads[$manifest.main_entry.file], 0, $payloads[$manifest.main_entry.file].Length)
$ms.Write($original, $mainOffset + $mainLength, $pyzOffset - ($mainOffset + $mainLength))
$ms.Write($payloads[$manifest.pyz.header_file], 0, $payloads[$manifest.pyz.header_file].Length)
$ms.Write($original, $pyzOffset + $pyzHeaderLength, $pyzTocOffset - $pyzHeaderLength)
$ms.Write($payloads[$manifest.pyz.append_file], 0, $payloads[$manifest.pyz.append_file].Length)
$ms.Write($payloads[$manifest.pyz.toc_file], 0, $payloads[$manifest.pyz.toc_file].Length)
$ms.Write($payloads[$manifest.carchive.toc_file], 0, $payloads[$manifest.carchive.toc_file].Length)
$ms.Write($payloads[$manifest.carchive.cookie_file], 0, $payloads[$manifest.carchive.cookie_file].Length)
$patched = $ms.ToArray()
$ms.Dispose()

if ($patched.Length -ne [int]$manifest.patched.size) {
    Fail "拼出来的 exe 大小不对（$($patched.Length) ≠ $($manifest.patched.size)），没有动原来的文件。"
}
if ((Bytes-Hash $patched) -ne $manifest.patched.sha256) {
    Fail "拼出来的 exe 校验不过，没有动原来的文件。你这份 exe 可能不是官方 v$version。"
}

if (-not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $target -Destination $backup
    Say "备份：$backup"
}
$tmp = "$target.new"
[IO.File]::WriteAllBytes($tmp, $patched)
Move-Item -LiteralPath $tmp -Destination $target -Force
Say '补丁打好了。'
Say ''
Say '接着在程序里设置：设置 → 模型 → 判断 · Jev → 来源选「自定义 · OpenAI 兼容」'
Say '→ 填 Base URL（跟 OpenAI 一样，一般要带 /v1）→ 填这一家的密钥（换来源要重填一次）'
Say '→ 点「获取模型」挑一个，或者自己把模型 id 打进去 → 保存设置。'
