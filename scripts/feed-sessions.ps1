# 多会话批量投喂：遍历侧栏里的会话，空闲的补一条续派指令
#
# 判定（都靠无障碍树，不需要前台焦点，锁屏可用）：
#   * 出现 busy 按钮文案（默认「停止生成」）      → 该会话正在出字，跳过
#   * 出现 queued 按钮文案（默认「移除待发送消息」）→ 队列里已有待发指令，跳过
#   * 其余 → 聚焦输入框、逐字投递 ASCII、点发送
#
# 会话怎么选（防误伤，二选一）：
#   * -Targets '会话A|会话B'   按名字精确匹配（推荐，面板里勾选会话生成）
#   * -SessionRegex '正则'     按正则匹配会话名
#   * 两个都不给 → 拒绝投喂（exit 3）：默认全喂会把指令灌进每一个对话
#
# 用法：
#   pwsh -File feed-sessions.ps1 -Prompt "Continue the task." -Targets 'Supervisor: zburn'
#   pwsh -File feed-sessions.ps1 -Prompt "..." -SessionRegex '项目名|Task:' -ProcessName ZCode
#   pwsh -File feed-sessions.ps1 -Prompt "x" -DryRun -Json   # 列出侧栏全部会话（面板扫描用）
#
# 退出码：0 正常（无论喂了几条）/ 1 没窗口 / 3 没指定会话筛选
param(
    # 真正投喂时必填；扫描模式（-DryRun -Json）不需要
    [string]$Prompt = '',
    [string]$ProcessName = 'ZCode',
    # 同一个客户端开了多个窗口时，用窗口标题挑一个；留空 = 第一个有主窗口的进程
    [string]$WindowTitleRegex = '',
    [string]$SessionRegex = '.',
    [string]$Targets = '',
    [string]$SendButtonRegex = '发送|加入队列|立即|Send',
    [string]$BusyRegex = '停止生成|Stop generating',
    [string]$QueuedRegex = '移除待发送消息|Remove queued',
    [int]$MaxSessions = 6,
    [int]$CharDelayMs = 18,
    [switch]$DryRun,
    [switch]$Json,
    [int]$MaxDepth = 40
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$WM_CHAR = 0x0102
$AT = [System.Windows.Automation.AutomationElement]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

Add-Type -Namespace Zburn -Name Native2 -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@
Add-Type -Namespace Zburn -Name Win -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
public delegate bool EnumProc(IntPtr h, IntPtr lp);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int max);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
"@

# 目标窗口：给了标题正则就枚举该进程全部顶层窗口按标题挑；
# 没给就沿用旧行为（第一个有主窗口的进程的主窗口）
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
    else { [Console]::Error.WriteLine("未找到 $ProcessName 主窗口") }
    exit 1
}
$top = $win.H
$root = $AT::FromHandle($top)
# -Json 是给面板解析的，stdout 必须只有 JSON
if (-not $Json) { Write-Output ("目标窗口: {0}" -f $win.Title) }

function Get-Controls([switch]$WithClass) {
    $acc = New-Object System.Collections.ArrayList
    function Walk($el, $d) {
        if ($d -gt $MaxDepth) { return }
        try {
            $ct = $el.Current.ControlType
            $r = $el.Current.BoundingRectangle
            # ClassName 只在扫描时取（项目归属、当前对话靠它）；
            # 投喂循环不读，省下每轮几百次 UIA 属性调用
            $cls = ''
            if ($WithClass -and $ct.ProgrammaticName -match 'ListItem|Group') {
                try { $cls = $el.Current.ClassName } catch { }
            }
            [void]$acc.Add([pscustomobject]@{
                El = $el; Type = $ct.ProgrammaticName; Name = $el.Current.Name; Class = $cls
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

# 会话名末尾粘着相对时间（"刚刚" / "2分" / "49天"），一分钟后就变了。
# 所以匹配前两边都剥掉它：否则存下来的名字下一轮就匹配不上。
function Get-SessionKey([string]$name) {
    if (-not $name) { return '' }
    return ($name -replace '(刚刚|\s*\d+\s*(秒|秒钟|分|分钟|小时|天|周|个月|月|年))$', '').Trim()
}

$all = Get-Controls -WithClass:($DryRun -and $Json)
# 会话候选：侧栏里的列表项（ZCode 用 ListItem，DSH 用 TreeItem），名字非空、位于窗口左侧 45% 内
$winW = ($all | Where-Object { $_.Type -eq 'ControlType.Window' -and $_.W -gt 600 } | Select-Object -First 1).W
if (-not $winW) { $winW = 1920 }
$sideItems = @($all | Where-Object {
        ($_.Type -eq 'ControlType.ListItem' -or $_.Type -eq 'ControlType.TreeItem') -and $_.Name -and
        ($_.X -lt $winW * 0.45) -and $_.W -gt 40 -and $_.H -gt 12
    })
foreach ($s in $sideItems) {
    $s | Add-Member -NotePropertyName Key -NotePropertyValue (Get-SessionKey $s.Name) -Force
}

# 侧栏里会出现同名会话（实测同一条标题能有 2~3 个），而 UIA 不提供稳定的会话 ID
# （AutomationId 为空，RuntimeId 重启就变）。所以同名会话按纵向顺序编号，
# 身份用「标题#序号」：这样「标记第 2 条」不会连带喂到第 1 条。
$byKey = @{}
foreach ($s in $sideItems) {
    if (-not $byKey.ContainsKey($s.Key)) {
        $byKey[$s.Key] = New-Object System.Collections.ArrayList
    }
    [void]$byKey[$s.Key].Add($s)
}
foreach ($kv in $byKey.GetEnumerator()) {
    $group = @($kv.Value | Sort-Object Y)
    for ($i = 0; $i -lt $group.Count; $i++) {
        $group[$i] | Add-Member -NotePropertyName Ord -NotePropertyValue ($i + 1) -Force
        $group[$i] | Add-Member -NotePropertyName GroupSize -NotePropertyValue $group.Count -Force
        # 唯一的会话就用裸标题（配置里好读）；同名才带序号
        $id = $group[$i].Key
        if ($group.Count -gt 1) { $id = "{0}#{1}" -f $group[$i].Key, ($i + 1) }
        $group[$i] | Add-Member -NotePropertyName Id -NotePropertyValue $id -Force
    }
}

# 扫描模式（面板用）：侧栏全部会话原样列出、不做筛选，调用方自己挑
if ($DryRun -and $Json) {
    # 项目归属与「当前对话」：
    #   * 项目头在 UIA 里是 ClassName 含 project-row 的 Group，它下面的会话都属于该项目
    #     （会话和项目头是兄弟节点，没有父子关系，只能按纵向位置就近归属）；
    #   * 当前打开的对话没有 SelectionItemPattern，ZCode 是给 ListItem 挂 bg-selected class，
    #     只能靠 ClassName 认。
    $projectRows = @($all | Where-Object { $_.Type -eq 'ControlType.Group' -and $_.Class -match 'project-row' })
    $projects = @(foreach ($pr in $projectRows) {
            $btn = $all | Where-Object {
                $_.Type -eq 'ControlType.Button' -and $_.W -gt 100 -and $_.Name -and
                $_.Y -ge $pr.Y -and $_.Y -lt ($pr.Y + $pr.H) -and
                $_.X -ge $pr.X -and $_.X -lt ($pr.X + 300)
            } | Sort-Object X | Select-Object -First 1
            if ($btn) { [pscustomobject]@{ Y = [double]$pr.Y; Name = $btn.Name } }
        })
    foreach ($s in $sideItems) {
        $proj = $projects | Where-Object { $_.Y -le $s.Y } | Sort-Object Y -Descending | Select-Object -First 1
        $s | Add-Member -NotePropertyName Project -NotePropertyValue $(if ($proj) { $proj.Name } else { '' }) -Force
        $s | Add-Member -NotePropertyName Active -NotePropertyValue ($s.Class -match 'bg-selected') -Force
    }
    $out = @($sideItems | ForEach-Object {
            @{ name = $_.Name; key = $_.Key; id = $_.Id; ord = $_.Ord; count = $_.GroupSize
                project = $_.Project; active = [bool]$_.Active
                x = [int]$_.X; y = [int]$_.Y; w = [int]$_.W; h = [int]$_.H }
        })
    ConvertTo-Json -InputObject $out -Depth 3
    exit 0
}

# 会话筛选：Targets（监控名单）优先，其次 SessionRegex；
# 两个都没有就拒绝——空筛选等于「每个对话都喂」，实测会往所有会话里灌指令
#
# -Targets 接受两种写法：JSON 数组（面板/守护进程用，标题里有逗号/竖线也不会被切坏）
# 或竖线分隔（手工命令行 -Targets 'A|B'）
$rawTargets = @()
if ($Targets -and $Targets.TrimStart().StartsWith('[')) {
    try { $rawTargets = @($Targets | ConvertFrom-Json) } catch { $rawTargets = @() }
} elseif ($Targets) {
    $rawTargets = @($Targets -split '[\|,，\r\n]')
}
$rawTargets = @($rawTargets | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })

# 解析每条名单：整串能对上标题就用整串（不区分同名，写全名就是想喂这一组）；
# 对不上再按「标题#序号」拆开精确匹配某一条。
# 这样手写「issue #42」这类带井号的标题也不会被误拆。
$knownKeys = @($sideItems | ForEach-Object { $_.Key } | Select-Object -Unique)
$specs = New-Object System.Collections.ArrayList
$missed = @()
foreach ($raw in $rawTargets) {
    $norm = Get-SessionKey $raw
    if ($knownKeys -contains $norm) {
        [void]$specs.Add([pscustomobject]@{ Key = $norm; Ord = 0; Raw = $raw })
        continue
    }
    $m = [regex]::Match($raw, '^(.*)#(\d+)$')
    if ($m.Success) {
        $mk = Get-SessionKey $m.Groups[1].Value
        if ($knownKeys -contains $mk) {
            [void]$specs.Add([pscustomobject]@{ Key = $mk; Ord = [int]$m.Groups[2].Value; Raw = $raw })
            continue
        }
    }
    $missed += $raw
}

if ($rawTargets.Count -gt 0) {
    # 给了名单却一条都没对上 → 直接拒绝。绝不能退回正则/全量，
    # 否则「对话改名/被删」就会变成往每个对话里灌指令（这就是要修的那个事故）。
    if ($specs.Count -eq 0) {
        [Console]::Error.WriteLine("监控名单里的对话一个都没在侧栏找到 → 不投喂：{0}" -f ($missed -join ' | '))
        exit 3
    }
    $tasks = @($sideItems | Where-Object {
            $item = $_
            @($specs | Where-Object { $_.Key -eq $item.Key -and ($_.Ord -eq 0 -or $_.Ord -eq $item.Ord) }).Count -gt 0
        })
    Write-Output ("候选会话 {0} 个（监控名单 {1} 条）" -f $tasks.Count, $specs.Count)
} else {
    if (-not $SessionRegex) {
        [Console]::Error.WriteLine('未指定要投喂的会话（监控名单为空、会话正则也为空）→ 拒绝全量投喂')
        exit 3
    }
    $tasks = @($sideItems | Where-Object { $_.Name -match $SessionRegex })
    $missed = @()
    Write-Output ("候选会话 {0} 个（正则 /{1}/）" -f $tasks.Count, $SessionRegex)
}
if ($missed.Count -gt 0) { Write-Output ("侧栏没找到这些对话: {0}" -f ($missed -join ' | ')) }
if ($DryRun) {
    foreach ($t in $tasks) { Write-Output ("  - {0}  ({1},{2} {3}x{4})" -f $t.Name, [int]$t.X, [int]$t.Y, [int]$t.W, [int]$t.H) }
    exit 0
}
if (-not $Prompt) {
    [Console]::Error.WriteLine('没有 -Prompt：真要投喂必须给指令（只扫描请加 -DryRun）')
    exit 3
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

    # 选「离输入框最近」的那个发送按钮：ZCode 的内置浏览器面板右侧还有一个发送按钮，
    # 按 X 倒序会点到那个，字打进去了但消息永远发不出去。flush-composer.ps1 用同一套判据。
    $cRight = $composer.X + $composer.W
    $send = Get-Controls | Where-Object {
            $_.Type -eq 'ControlType.Button' -and $_.Name -match $SendButtonRegex -and
            $_.Name -notmatch $QueuedRegex -and $_.Name -notmatch $BusyRegex
        } | Sort-Object { [Math]::Abs($_.X - $cRight) + [Math]::Abs($_.Y - $composer.Y) } |
        Select-Object -First 1
    $ok = $false
    # 以前这里是 -gt 10：结果 "continue"(8 字符)、"go on"(5) 这类短指令永远发不出去，
    # 日志里只会看到「投喂失败 typed=8」。只要字真的打进去了就该点发送。
    if ($send -and $typed -gt 0) {
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
