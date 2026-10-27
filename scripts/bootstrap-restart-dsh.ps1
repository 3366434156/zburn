# 带无障碍开关重启 DSH，然后接管整夜调度。
# 由 DSH 自己拉起（独立进程），所以 DSH 被杀掉也不影响它继续跑。
$ErrorActionPreference = 'Continue'
$root = 'E:\数据\projects\zburn'
$log = "$root\.zburn\bootstrap.log"
$exe = 'E:\exe\DeepSeek Harness\DeepSeek Harness.exe'

function L($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $m"
    $line | Out-File -FilePath $log -Append -Encoding utf8
    Write-Output $line
}

L 'bootstrap: start (delay 25s so the agent can finish its reply)'
Start-Sleep -Seconds 25

# 1) 网关：ZCode 和 DSH 的模型都走 zcode2api，它掉了两边都得停
try {
    $meta = (Invoke-WebRequest 'http://127.0.0.1:3000/meta' -TimeoutSec 5 -UseBasicParsing).Content
    L "gateway alive: $meta"
} catch {
    L 'gateway down -> ensure'
    & python "$root\zburn.py" --config "$root\config.json" gateway 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 10
}

# 2) 结束 DSH（只杀这个镜像，不连带子进程，本脚本才活得下来）
L 'stopping DeepSeek Harness'
Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
    -ArgumentList '/IM', 'DeepSeek Harness.exe', '/F' -Wait -WindowStyle Hidden
Start-Sleep -Seconds 5

# 3) 带着无障碍开关重新拉起（先清掉会污染 Electron 的环境变量）
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
L 'starting DeepSeek Harness with --force-renderer-accessibility'
Start-Process -FilePath $exe -ArgumentList '--force-renderer-accessibility' -WorkingDirectory (Split-Path $exe)

# 4) 等无障碍树真的读到聊天区
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 5
    $out = & pwsh -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\uia-tree.ps1" `
        -ProcessName 'DeepSeek Harness' -Filter 'Edit|Button' -Json 2>$null
    $n = @($out).Count
    if ($n -gt 3) { $ok = $true; L "a11y ready: $n lines"; break }
    L "waiting a11y ($i): $n lines"
}
L "a11y ok=$ok"

# 5) 打印 agent 视图，然后起唯一调度器
& python "$root\zburn.py" --config "$root\config.json" agents 2>&1 |
    Out-File -FilePath $log -Append -Encoding utf8

L 'starting scheduler (run)'
Start-Process -FilePath 'pythonw' -ArgumentList "$root\zburn.py", '--config', "$root\config.json", 'run' `
    -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 10
L 'bootstrap: done'
