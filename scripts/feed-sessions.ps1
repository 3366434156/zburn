# 多会话批量投喂：遍历侧栏里的会话，空闲的补一条续派指令
#
# 判定（都靠无障碍树，不需要前台焦点，锁屏可用）：
#   * 出现 busy 按钮文案（默认「停止生成」）      → 该会话正在出字，跳过
#   * 出现 queued 按钮文案（默认「移除待发送消息」）→ 队列里已有待发指令，跳过
#   * 其余 → 聚焦输入框、逐字投递 ASCII、点发送
#
# 用法：
#   pwsh -File feed-sessions.ps1 -Prompt "Continue the task." [-MaxSessions 5] [-DryRun]
#   pwsh -File feed-sessions.ps1 -Prompt "..." -SessionRegex '项目名|Task:' -ProcessName ZCode
#
# 退出码：0 正常（无论喂了几条）/ 1 没窗口
param(
    [Parameter(Mandatory = $true)][string]$Prompt,
    [string]$ProcessName = 'ZCode',
    [string]$SessionRegex = '.',
    [string]$SendButtonRegex = '发送|加入队列|立即|Send',
    [string]$BusyRegex = '停止生成|Stop generating',
    [string]$QueuedRegex = '移除待发送消息|Remove queued',
    [int]$MaxSessions = 6,
    [int]$CharDelayMs = 18,
    [switch]$DryRun,
    [int]$MaxDepth = 40
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace Zburn -Name Native2 -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@

$WM_CHAR = 0x0102
$AT = [System.Windows.Automation.AutomationElement]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("未找到 $ProcessName 主窗口"); exit 1 }
$top = $proc.MainWindowHandle
$root = $AT::FromHandle($top)

function Get-Controls {
    $acc = New-Object System.Collections.ArrayList
    function Walk($el, $d) {
        if ($d -gt $MaxDepth) { return }
        try {
            $ct = $el.Current.ControlType
            $r = $el.Current.BoundingRectangle
            [void]$acc.Add([pscustomobject]@{
                El = $el; Type = $ct.ProgrammaticName; Name = $el.Current.Name
                X = [double]$r.X; Y = [double]$r.Y; W = [double]$r.Width; H = [double]$r.Height
            })
        } catch { return }
        try { $c = $walker.GetFirstChild($el) } catch { return }
        while ($c) {
            Walk $c ($d + 1)
            try { $c = $walker.GetNextSibling($c) } catch { break }
        }
    }
    Walk $root 0
    return $acc
}

function Find-Composer($items) {
    $best = $null; $bestArea = 0
    foreach ($c in $items) {
        if ($c.Type -ne 'ControlType.Edit' -or $c.W -lt 60 -or $c.H -lt 18) { continue }
        $area = $c.W * $c.H
        if ($area -gt $bestArea) { $bestArea = $area; $best = $c }
    }
    return $best
}

$all = Get-Controls
# 会话候选：侧栏里的 ListItem（名字非空、位于窗口左侧 40% 内）
$winW = ($all | Where-Object { $_.Type -eq 'ControlType.Window' -and $_.W -gt 600 } | Select-Object -First 1).W
if (-not $winW) { $winW = 1920 }
$tasks = @($all | Where-Object {
        $_.Type -eq 'ControlType.ListItem' -and $_.Name -and $_.Name -match $SessionRegex -and
        ($_.X -lt $winW * 0.45) -and $_.W -gt 40 -and $_.H -gt 12
    })
Write-Output ("候选会话 {0} 个（侧栏 ListItem，匹配 /{1}/）" -f $tasks.Count, $SessionRegex)
if ($DryRun) {
    foreach ($t in $tasks) { Write-Output ("  - {0}  ({1},{2} {3}x{4})" -f $t.Name, [int]$t.X, [int]$t.Y, [int]$t.W, [int]$t.H) }
    exit 0
}

$done = 0; $skipped = 0; $failed = 0
foreach ($t in $tasks) {
    if ($done -ge $MaxSessions) { break }
    $label = $t.Name.Substring(0, [Math]::Min(26, $t.Name.Length))
    try { $t.El.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
    catch {
        try { $t.El.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        catch { Write-Output ("  [$label] 切换失败"); $failed++; continue }
    }
    Start-Sleep -Milliseconds 1600

    $items = Get-Controls
    $busy = @($items | Where-Object { $_.Type -eq 'ControlType.Button' -and $_.Name -match $BusyRegex }).Count -gt 0
    $queued = @($items | Where-Object { $_.Type -eq 'ControlType.Button' -and $_.Name -match $QueuedRegex }).Count -gt 0
    if ($busy -or $queued) {
        Write-Output ("  [$label] 跳过（busy={0} queued={1}）" -f $busy, $queued)
        $skipped++
        continue
    }

    # 切换会话后界面渲染有延迟（多会话并行时更慢），最多等 8 秒
    $composer = $null
    for ($wait = 0; $wait -lt 8 -and -not $composer; $wait++) {
        $composer = Find-Composer (Get-Controls)
        if (-not $composer) { Start-Sleep -Milliseconds 1000 }
    }
    if (-not $composer) { Write-Output ("  [$label] 未找到输入框"); $failed++; continue }
    try { $composer.El.SetFocus() } catch { }

    $value = $composer.El.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $len = { try { $value.Current.Value.Length } catch { 0 } }
    # 输入框里已有内容 -> 先清空（避免把新指令拼在旧文本后面）
    $round = 0
    while ((& $len) -gt 1 -and $round -lt 4) {
        for ($i = 0; $i -lt (& $len) + 2; $i++) {
            [void][Zburn.Native2]::PostMessage($top, $WM_CHAR, [IntPtr]0x08, [IntPtr]1)
        }
        Start-Sleep -Milliseconds 300
        $round++
    }

    $baseLen = (& $len) - 1
    $clean = (($Prompt -replace "[`r`n]+", ' ').ToCharArray() |
        Where-Object { [int][char]$_ -ge 32 -and [int][char]$_ -le 126 }) -join ''
    foreach ($ch in $clean.ToCharArray()) {
        [void][Zburn.Native2]::PostMessage($top, $WM_CHAR, [IntPtr][int][char]$ch, [IntPtr]1)
        Start-Sleep -Milliseconds $CharDelayMs
    }
    Start-Sleep -Milliseconds 500
    $typed = (& $len) - 1 - $baseLen

    $send = Get-Controls | Where-Object {
            $_.Type -eq 'ControlType.Button' -and $_.Name -match $SendButtonRegex -and
            $_.Name -notmatch $QueuedRegex -and $_.Name -notmatch $BusyRegex
        } | Sort-Object X -Descending | Select-Object -First 1
    $ok = $false
    if ($send -and $typed -gt 10) {
        try {
            $send.El.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            Start-Sleep -Milliseconds 800
            $ok = ((& $len) - 1 -le $baseLen)
        } catch { }
    }
    if ($ok) { Write-Output ("  [$label] 已投喂 {0} 字符" -f $typed); $done++ }
    else { Write-Output ("  [$label] 投喂失败 typed={0} send={1}" -f $typed, ($null -ne $send)); $failed++ }
}
Write-Output ("汇总：投喂 {0} / 跳过 {1} / 失败 {2}" -f $done, $skipped, $failed)
