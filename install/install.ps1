$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$zipUrl  = if ($env:PRL_PKG_URL) { $env:PRL_PKG_URL } else { "https://github.com/USER/pearl-self-dashboard/releases/latest/download/prl.zip" }
$token   = $env:PRL_TOKEN
$wallet  = $env:PRL_WALLET
if ($env:PRL_WORKER) { $worker = $env:PRL_WORKER } else { $worker = "rig-a" }
$base    = Join-Path $env:USERPROFILE "prl"
$dashboardUrl = if ($env:PRL_DASHBOARD) { $env:PRL_DASHBOARD } else { "http://127.0.0.1:8088" }
Write-Host ""
Write-Host "===== PRL 控制台 安装向导 =====" -ForegroundColor Cyan
if (-not $wallet)  { $script:wallet  = Read-Host "请输入你的 PRL 收款地址 (prl1...)" }
if (-not $token)   { $script:token   = Read-Host "请输入上报令牌（看板服务端 data/.rig_token）" }
if (-not $env:PRL_WORKER) {
  $w = Read-Host "这台机器的名字（默认 rig-a，多台机器请取不同名字）"
  if ($w) { $script:worker = $w }
}
if (-not $dashboardUrl -or $dashboardUrl -eq "http://127.0.0.1:8088") {
  $d = Read-Host "看板地址（默认 http://127.0.0.1:8088，用了服务器就填服务器地址）"
  if ($d) { $script:dashboardUrl = $d }
}
if (-not $wallet -or -not $token) { Write-Host "[X] 收款地址和令牌必须填写" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "===== PRL 挖矿 · 一键安装（矿工名 $worker）=====" -ForegroundColor Cyan

if (-not (Get-Command python.exe -ErrorAction SilentlyContinue) -and -not (Get-Command py.exe -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"))) {
  Write-Host "[X] 没找到 Python。" -ForegroundColor Red
  Write-Host "    先装 DeepSeek Harness（自带运行时），或去 python.org 装 Python 3.12（勾选 Add to PATH）。"
  return
}

$cands = @()
$c1 = Get-Command python.exe -ErrorAction SilentlyContinue; if ($c1) { $cands += $c1.Source }
$c2 = Get-Command py.exe -ErrorAction SilentlyContinue;     if ($c2) { $cands += $c2.Source }
$cands += (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")
$py = $null
foreach ($c in $cands) { if ($c -and (Test-Path $c)) { $py = $c; break } }
$pyw = $py -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "[OK] Python: $py"

# 0) 如果已经在跑，先停掉（否则 miner.exe 被占用无法覆盖）
try {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*guardian.py*' -or $_.CommandLine -like '*rig_agent.py*' -or $_.CommandLine -like '*panel.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-Process miner -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
} catch {}
New-Item -ItemType Directory -Force -Path $base | Out-Null
$tmp = Join-Path $env:TEMP "prl.zip"
Write-Host "[..] 下载安装包（约 6.5MB）..."
Invoke-WebRequest -Uri $zipUrl -OutFile $tmp -UseBasicParsing
Expand-Archive -Path $tmp -DestinationPath $base -Force
Remove-Item $tmp -Force
Write-Host "[OK] 已解压到 $base"

$cfgObj = [ordered]@{
  url       = $dashboardUrl.TrimEnd("/") + "/api/rig"
  token     = $token
  worker    = $worker
  wallet    = $wallet
  miner_log = (Join-Path $base "official.log.err")
}
$json = $cfgObj | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $base "agent\config.json"), $json, (New-Object Text.UTF8Encoding($false)))
Write-Host "[OK] 配置完成（矿工名 $worker，收益单独记）"

if (-not (Test-Path (Join-Path $base "miner\miner.exe"))) {
  Write-Host "[!] miner.exe 不见了 —— 被杀毒软件删了。" -ForegroundColor Yellow
  Write-Host "    把下面这个目录加进杀软信任区，然后重新运行本脚本："
  Write-Host "    $base"
}

$ws = New-Object -ComObject WScript.Shell
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "PRL 控制台.lnk"
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $pyw
$sc.Arguments = '"' + (Join-Path $base "panel.py") + '"'
$sc.WorkingDirectory = $base
$sc.IconLocation = (Join-Path $iconDir "prl.ico") + ",0"
$sc.Description = "PRL 挖矿控制台"
$sc.Save()
Write-Host "[OK] 桌面快捷方式：PRL 控制台"

if (Test-Path (Join-Path $base "miner\miner.exe")) {
  Start-Process -FilePath $py -ArgumentList "pearlctl.py","start" -WorkingDirectory $base -WindowStyle Hidden
  Start-Sleep -Seconds 4
}
Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $base "panel.py") + '"') -WorkingDirectory $base
Write-Host ""
Write-Host "===== 安装完成 =====" -ForegroundColor Green
Write-Host "1) 桌面双击「PRL 控制台」看状态（算力/温度/过去24小时/开关）"
Write-Host "2) 网页看板：$dashboardUrl/"
Write-Host "3) 打游戏自动暂停；超过 80°C 持续 1 分钟自动停矿，降到 70°C 自动恢复"
Write-Host ""

# ---------- 开机自启（F11）----------
try {
    $startup = [Environment]::GetFolderPath("Startup")
    $auto = Join-Path $startup "PRL 控制台 开机自启.lnk"
    $ws2 = New-Object -ComObject WScript.Shell
    $sc2 = $ws2.CreateShortcut($auto)
    $sc2.TargetPath = $pyw
    $sc2.Arguments = '"' + (Join-Path $base "guardian.py") + '"'
    $sc2.WorkingDirectory = $base
    $sc2.IconLocation = "$($sc.IconLocation)"
    $sc2.Description = "开机自动开始挖矿（无窗口）"
    $sc2.Save()
    Write-Host "[OK] 已设置开机自启（想取消就删掉启动文件夹里的 lnk）"
} catch {
    Write-Host "[!] 开机自启创建失败：$($_.Exception.Message)"
}
