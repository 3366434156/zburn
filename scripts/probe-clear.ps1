# 试验：对「聊天输入框」逐个试清空手段，看哪个真的有效（锁屏下）
param([string]$ProcessName = 'ZCode')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace ZburnProbe2 -Name N -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { 'no window'; exit 1 }
$top = $proc.MainWindowHandle
$root = [System.Windows.Automation.AutomationElement]::FromHandle($top)

$script:best = $null
$script:area = 0
function W($el, $d) {
    if ($d -gt 40) { return }
    try {
        if ($el.Current.ControlType.ProgrammaticName -eq 'ControlType.Edit') {
            $r = $el.Current.BoundingRectangle
            $a = [double]$r.Width * [double]$r.Height
            if ($a -gt $script:area) { $script:area = $a; $script:best = $el }
        }
    } catch { }
    try { $c = $walker.GetFirstChild($el) } catch { return }
    while ($c) { W $c ($d + 1); try { $c = $walker.GetNextSibling($c) } catch { break } }
}
W $root 0
if (-not $script:best) { 'no edit'; exit 1 }
$edit = $script:best
$r = $edit.Current.BoundingRectangle
"选中输入框: ({0},{1}) {2}x{3}（面积 {4}）" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, [int]$script:area
$val = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
$read = { try { $val.Current.Value } catch { '' } }
$edit.SetFocus(); Start-Sleep -Milliseconds 250
"当前内容: '$(((& $read) -replace "`r?`n", ' '))'"
""
function TryClear([string]$name, [scriptblock]$action) {
    $before = (& $read).Length
    & $action
    Start-Sleep -Milliseconds 600
    $after = (& $read).Length
    "  {0,-26} len {1} -> {2}   {3}" -f $name, $before, $after, $(if ($after -lt $before) { '有效' } else { '无效' })
}
TryClear 'WM_CHAR 0x08 ×N' {
    $n = [Math]::Min((& $read).Length + 2, 120)
    for ($i = 0; $i -lt $n; $i++) {
        [void][ZburnProbe2.N]::PostMessage($top, 0x0102, [IntPtr]0x08, [IntPtr]1)
        Start-Sleep -Milliseconds 25
    }
}
TryClear 'VK_BACK keydown/up ×N' {
    $n = [Math]::Min((& $read).Length + 2, 120)
    for ($i = 0; $i -lt $n; $i++) {
        [void][ZburnProbe2.N]::PostMessage($top, 0x0100, [IntPtr]0x08, [IntPtr]0x000E0001)
        [void][ZburnProbe2.N]::PostMessage($top, 0x0101, [IntPtr]0x08, [IntPtr]0xC00E0001)
        Start-Sleep -Milliseconds 25
    }
}
"剩余内容: '$(((& $read) -replace "`r?`n", ' '))'"
