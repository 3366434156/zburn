# 无障碍树探针：把目标窗口的 UIA 控件树 dump 出来（JSON / 表格）
#
# 换一个应用（不是 ZCode）要用这套熄屏注入时，先用它摸清控件：
#   * 哪个是聊天输入框（通常是面积最大的 Edit）
#   * 发送/停止/排队按钮叫什么名字
#   * 会话列表项是不是 ListItem、名字长什么样
#
# 用法：
#   pwsh -File uia-tree.ps1                       # 表格输出，默认 ZCode
#   pwsh -File uia-tree.ps1 -ProcessName Notepad -Json
#   pwsh -File uia-tree.ps1 -Filter 'Edit|Button' # 只看输入框和按钮
param(
    [string]$ProcessName = 'ZCode',
    [string]$WindowTitleRegex = '',
    [string]$Filter = '',
    [switch]$Json,
    [int]$MaxDepth = 40
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace Zburn -Name Win -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
public delegate bool EnumProc(IntPtr h, IntPtr lp);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int max);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
"@
$AT = [System.Windows.Automation.AutomationElement]
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

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
$root = $AT::FromHandle($win.H)

$rows = New-Object System.Collections.ArrayList
function Walk($el, $d, $path) {
    if ($d -gt $MaxDepth) { return }
    $name = ''; $type = ''; $x = 0; $y = 0; $w = 0; $h = 0; $enabled = $false
    try {
        $type = $el.Current.ControlType.ProgrammaticName -replace 'ControlType\.', ''
        $name = $el.Current.Name
        $r = $el.Current.BoundingRectangle
        $x = [int]$r.X; $y = [int]$r.Y; $w = [int]$r.Width; $h = [int]$r.Height
        $enabled = $el.Current.IsEnabled
    } catch { return }
    if (-not $Filter -or $type -match $Filter) {
        [void]$rows.Add([pscustomobject]@{
                depth = $d; type = $type; name = $name; x = $x; y = $y; w = $w; h = $h; enabled = $enabled
            })
    }
    try { $c = $walker.GetFirstChild($el) } catch { return }
    while ($c) {
        Walk $c ($d + 1) $path
        try { $c = $walker.GetNextSibling($c) } catch { break }
    }
}
Walk $root 0 ''

if ($Json) {
    $rows | ConvertTo-Json -Depth 4
} else {
    Write-Output ("{0,-4} {1,-14} {2,-30} {3}" -f 'depth', 'type', 'name', 'rect')
    foreach ($r in $rows) {
        $nm = $r.name
        if ($nm.Length -gt 28) { $nm = $nm.Substring(0, 28) + '…' }
        Write-Output ("{0,-4} {1,-14} {2,-30} ({3},{4} {5}x{6}){7}" -f
            $r.depth, $r.type, $nm, $r.x, $r.y, $r.w, $r.h, $(if (-not $r.enabled) { ' [disabled]' } else { '' }))
    }
    Write-Output ("共 {0} 个控件" -f $rows.Count)
}
