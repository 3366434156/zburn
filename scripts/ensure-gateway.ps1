# 确保网关进程在跑：只在「确实连不上」时才启动，绝不重启已在运行的副本
#
# 这是硬规矩：网关被杀 = 正在跑的对话全部断流（ZCode 里表现为 connection lost / turn failed）。
# 所以本脚本只做「探活 → 必要时启动」，不做 kill / restart。
#
# 用法：
#   pwsh -File ensure-gateway.ps1 -HealthUrl http://127.0.0.1:3000/meta -Command "E:\...\python.exe E:\...\run.py"
#   pwsh -File ensure-gateway.ps1 -HealthUrl ... -Command ... -GraceSeconds 60 -DryRun
#
# 输出：started / alive / waiting（进程在但还没起来）
param(
    [Parameter(Mandatory = $true)][string]$HealthUrl,
    [Parameter(Mandatory = $true)][string]$Command,
    [string]$WorkDir = '',
    [int]$GraceSeconds = 0,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 4 -UseBasicParsing
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

if (Test-Health) { Write-Output 'alive'; exit 0 }
if ($GraceSeconds -gt 0) {
    Write-Output "down，等 $GraceSeconds 秒再确认（避免把正在重启的网关判死）"
    Start-Sleep -Seconds $GraceSeconds
    if (Test-Health) { Write-Output 'alive'; exit 0 }
}
if ($DryRun) { Write-Output "dry-run: 将执行 $Command"; exit 0 }

Write-Output "down → 启动：$Command"
$parts = $Command -split '\s+', 2
$exe = $parts[0]
$argList = @()
if ($parts.Count -gt 1) { $argList = $parts[1] }
$sp = @{ FilePath = $exe; PassThru = $true; WindowStyle = 'Hidden' }
if ($argList.Count -gt 0) { $sp.ArgumentList = $argList }
if ($WorkDir) { $sp.WorkingDirectory = $WorkDir }
$p = Start-Process @sp

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Health) { Write-Output ("started pid={0}" -f $p.Id); exit 0 }
}
Write-Output ("waiting pid={0}（进程起了但健康检查还没通）" -f $p.Id)
exit 0
