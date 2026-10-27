# 熄屏注入器（UIA + PostMessage）——不需要前台焦点，锁屏也能把文本送进输入框并提交
#
# 为什么不用 SendKeys / 前台点击：
#   * 锁屏时 SetForegroundWindow 一定失败，SendKeys 会把字符打进锁屏密码框（危险）；
#   * 窗口被遮挡/最小化时鼠标注入会打偏。
# 本脚本走 UI Automation 的 ValuePattern / InvokePattern + 目标窗口的 WM_CHAR，全程不抢焦点。
#
# 实测结论（ZCode 2.x，2026-10 复核）：
#   1. WM_CHAR 逐字投递**能用**（每字 20~25ms，太快会丢字）；
#   2. 提交靠 Invoke「发送/加入队列」按钮最稳，回车只是兜底；
#   3. **退格清不掉内容**（WM_CHAR 0x08 与 VK_BACK 实测都无效）——
#      所以本脚本改成「内容比对 + 只补缺失后缀」，永远不需要清空：
#        · 输入框空          → 从头打
#        · 已是本指令的前缀  → 只补剩下的后缀（上次打一半失败的情况）
#        · 是别的残留内容    → 直接跳过并报错（exit 7），绝不往别人的文本上追加
#   4. WM_CHAR 只可靠支持 ASCII（CJK 会被映射成乱码）→ 调用方必须传英文/ASCII。
#
# 用法：
#   pwsh -File inject-uia.ps1 -Text "Continue the task." [-NoEnter] [-DryRun]
#   pwsh -File inject-uia.ps1 -Text "..." -ProcessName ZCode -SendButtonRegex '发送|加入队列|Send'
#
# 退出码：0 成功 / 1 没窗口 / 2 没输入框 / 4 投递不完整 / 5 未提交 / 7 输入框里有别的残留
param(
    [Parameter(Mandatory = $true)][string]$Text,
    [string]$ProcessName = 'ZCode',
    # 同一个客户端开了多个窗口时，用窗口标题挑一个；留空 = 第一个有主窗口的进程
    [string]$WindowTitleRegex = '',
    [string]$SendButtonRegex = '发送|加入队列|立即|Send',
    # 「移除待发送消息」「停止生成」里也含"发送/生成"，必须排除，否则会点错按钮
    [string]$ExcludeButtonRegex = '移除待发送消息|停止生成|Remove queued|Stop generating',
    [switch]$NoEnter,
    [switch]$DryRun,
    [int]$CharDelayMs = 25,
    [int]$MaxDepth = 40
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace Zburn -Name Native -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@
Add-Type -Namespace Zburn -Name Win -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
public delegate bool EnumProc(IntPtr h, IntPtr lp);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int max);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
"@

$WM_CHAR = 0x0102
$AT = [System.Windows.Automation.AutomationElement]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

# ---------------------------------------------------------------- 找窗口
function Find-TargetWindow {
    param([string]$ProcName, [string]$TitleRegex)
    if ($TitleRegex) {
        $procIds = @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.Id })
        if ($procIds.Count -eq 0) { return $null }
        $wins = New-Object System.Collections.ArrayList
        $cb = [Zburn.Win+EnumProc] {
            param($h, $lp)
            try {
                if (-not [Zburn.Win]::IsWindowVisible($h)) { return $true }
                [uint32]$wpid = 0
                [void][Zburn.Win]::GetWindowThreadProcessId($h, [ref]$wpid)
                if ($procIds -contains [int]$wpid) {
                    $sb = New-Object System.Text.StringBuilder 512
                    [void][Zburn.Win]::GetWindowText($h, $sb, 512)
                    if ($sb.Length -gt 0) { [void]$wins.Add([pscustomobject]@{ H = $h; Title = $sb.ToString() }) }
                }
            } catch { }
            return $true
        }
        [void][Zburn.Win]::EnumWindows($cb, [IntPtr]::Zero)
        return @($wins | Where-Object { $_.Title -match $TitleRegex } | Select-Object -First 1)
    }
    $proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1
    if (-not $proc) { return $null }
    return [pscustomobject]@{ H = $proc.MainWindowHandle; Title = $proc.MainWindowTitle }
}

$win = Find-TargetWindow -ProcName $ProcessName -TitleRegex $WindowTitleRegex
if (-not $win) {
    if ($WindowTitleRegex) { [Console]::Error.WriteLine("没有标题匹配 /$WindowTitleRegex/ 的 $ProcessName 窗口") }
    else { [Console]::Error.WriteLine("未找到 $ProcessName 主窗口（进程在跑但没窗口？）") }
    exit 1
}
$top = $win.H
$root = $AT::FromHandle($top)

# ---------------------------------------------------------------- 枚举控件
function Get-Controls([int]$maxDepth) {
    $acc = New-Object System.Collections.ArrayList
    function Walk($el, $d) {
        if ($d -gt $maxDepth) { return }
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

# 输入框：面积最大的 Edit（聊天输入框永远比搜索框/地址栏大得多）
function Find-Composer($items) {
    $best = $null; $bestArea = 0
    foreach ($c in $items) {
        if ($c.Type -ne 'ControlType.Edit' -or $c.W -lt 120 -or $c.H -lt 20) { continue }
        $area = $c.W * $c.H
        if ($area -gt $bestArea) { $bestArea = $area; $best = $c }
    }
    return $best
}

function Find-SendButton($items, $composer) {
    $cands = @($items | Where-Object {
            $_.Type -eq 'ControlType.Button' -and $_.Name -match $SendButtonRegex -and
            $_.Name -notmatch $ExcludeButtonRegex
        })
    if ($cands.Count -eq 0 -or -not $composer) { return $null }
    $near = @($cands | Where-Object { ($_.X -ge $composer.X + $composer.W * 0.5) -or ($_.Y -ge $composer.Y) })
    if ($near.Count -gt 0) { return ($near | Sort-Object X -Descending | Select-Object -First 1) }
    return ($cands | Sort-Object X -Descending | Select-Object -First 1)
}

$items = Get-Controls $MaxDepth
$composer = Find-Composer $items
for ($try = 0; $try -lt 5 -and -not $composer; $try++) {
    Start-Sleep -Milliseconds 800
    $items = Get-Controls $MaxDepth
    $composer = Find-Composer $items
}
if (-not $composer) {
    [Console]::Error.WriteLine('未找到输入框：窗口可能还没渲染完，或无障碍树被禁用（见 docs/故障排查.md）')
    exit 2
}
$sendBtn = Find-SendButton $items $composer
$value = $composer.El.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
# 读当前内容（去掉尾部的换行；空输入框的值通常就是一个 "\n"）
$readText = {
    try { ($value.Current.Value -replace "[`r`n]+$", '') } catch { '' }
}

if ($DryRun) {
    $btnName = '<none>'
    if ($sendBtn) { $btnName = $sendBtn.Name }
    Write-Output ("dry-run composer=({0},{1} {2}x{3}) sendButton={4} currentLen={5}" -f
        [int]$composer.X, [int]$composer.Y, [int]$composer.W, [int]$composer.H, $btnName, (& $readText).Length)
    exit 0
}

# ---------------------------------------------------------------- 目标文本（只保留 ASCII）
$clean = (($Text -replace "[`r`n]+", ' ').ToCharArray() |
    Where-Object { [int][char]$_ -ge 32 -and [int][char]$_ -le 126 }) -join ''
$target = $clean.Length
if ($target -eq 0) { [Console]::Error.WriteLine('文本里没有可投递的 ASCII 字符'); exit 4 }

$composer.El.SetFocus()
Start-Sleep -Milliseconds 250

# ---------------------------------------------------------------- 内容比对，只补缺失后缀
$current = & $readText
$prefixLen = 0
if ($current.Length -gt 0) {
    if ($clean.StartsWith($current)) {
        $prefixLen = $current.Length      # 上次打了一半，接着补
        Write-Output ("接续：输入框已有 {0} 字，补剩余 {1} 字" -f $prefixLen, ($target - $prefixLen))
    } else {
        [Console]::Error.WriteLine("输入框里有别的内容（{0} 字），不做追加以免污染：'{1}'" -f
            $current.Length, $current.Substring(0, [Math]::Min(40, $current.Length)))
        exit 7
    }
}

for ($attempt = 0; $attempt -lt 6; $attempt++) {
    $now = & $readText
    if ($now -eq $clean) { break }
    if (-not $clean.StartsWith($now)) {
        [Console]::Error.WriteLine("内容被外部改动，放弃本次投递：'{0}'" -f $now.Substring(0, [Math]::Min(40, $now.Length)))
        exit 7
    }
    $suffix = $clean.Substring($now.Length)
    for ($i = 0; $i -lt $suffix.Length; $i++) {
        [void][Zburn.Native]::PostMessage($top, $WM_CHAR, [IntPtr][int][char]$suffix[$i], [IntPtr]1)
        Start-Sleep -Milliseconds $CharDelayMs
        if (($i + 1) % 6 -eq 0) { Start-Sleep -Milliseconds 80 }
    }
    Start-Sleep -Milliseconds 450
}

$final = & $readText
$typedLen = $final.Length
if ($final -ne $clean) {
    [Console]::Error.WriteLine("投递不完整 typed=$typedLen target=$target")
    exit 4
}

# ---------------------------------------------------------------- 提交
$submitted = $false
if (-not $NoEnter) {
    if ($sendBtn) {
        try {
            $sendBtn.El.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            Start-Sleep -Milliseconds 700
            $submitted = ((& $readText).Length -eq 0)
        } catch { }
    }
    if (-not $submitted) {
        # 兜底：直接往窗口发回车（少量应用按钮不是 Invoke 型）
        [void][Zburn.Native]::PostMessage($top, 0x0100, [IntPtr]0x0D, [IntPtr]0x001C0001)
        Start-Sleep -Milliseconds 80
        [void][Zburn.Native]::PostMessage($top, 0x0101, [IntPtr]0x0D, [IntPtr]0xC01C0001)
        Start-Sleep -Milliseconds 700
        $submitted = ((& $readText).Length -eq 0)
    }
}
$btnOut = '<none>'
if ($sendBtn) { $btnOut = $sendBtn.Name }
Write-Output ("inject target={0} typed={1} submitted={2} button={3}" -f $target, $typedLen, $submitted, $btnOut)
if (-not $NoEnter -and -not $submitted) { exit 5 }
