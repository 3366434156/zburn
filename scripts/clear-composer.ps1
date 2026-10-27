# 尝试把聊天输入框里残留的测试文本清掉（清不掉就明确告诉人）
# 手段顺序：① 名字含 清除/清空 的按钮 → ② Escape 键 → ③ 报错，让人手动删
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -Namespace Zc -Name N -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@
$w = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$p = Get-Process -Name ZCode | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $p) { 'no window'; exit 1 }
$top = $p.MainWindowHandle
$root = [System.Windows.Automation.AutomationElement]::FromHandle($top)

$script:best = $null; $script:area = 0; $script:btns = New-Object System.Collections.ArrayList
function W($el, $d) {
    if ($d -gt 40) { return }
    try {
        $ct = $el.Current.ControlType.ProgrammaticName
        $r = $el.Current.BoundingRectangle
        if ($ct -eq 'ControlType.Edit') {
            $a = [double]$r.Width * [double]$r.Height
            if ($a -gt $script:area) { $script:area = $a; $script:best = $el }
        }
        if ($ct -eq 'ControlType.Button' -and $el.Current.Name) {
            [void]$script:btns.Add([pscustomobject]@{ El = $el; Name = $el.Current.Name; Y = [int]$r.Y; X = [int]$r.X })
        }
    } catch { }
    try { $c = $w.GetFirstChild($el) } catch { return }
    while ($c) { W $c ($d + 1); try { $c = $w.GetNextSibling($c) } catch { break } }
}
W $root 0
$e = $script:best
$v = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
$read = { ($v.Current.Value -replace "[`r`n]+$", '') }
"残留: '$(& $read)'"
$er = $e.Current.BoundingRectangle

# ① 输入框附近、名字像"清除"的按钮
$near = @($script:btns | Where-Object {
        $_.Name -match '清除|清空|重置|删除|Clear|Reset' -and
        [Math]::Abs($_.Y - [int]$er.Y) -lt 120 -and [Math]::Abs($_.X - [int]$er.X) -lt 900
    })
if ($near.Count -gt 0) {
    "找到候选清除按钮: $($near[0].Name)"
    try { $near[0].El.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 500 } catch { '  调用失败' }
    "尝试后: '$(& $read)'"
} else { "没有找到名字像清除的按钮（附近按钮: $((@($script:btns | Where-Object { [Math]::Abs($_.Y - [int]$er.Y) -lt 120 }) | ForEach-Object { $_.Name }) -join ', '))" }

if ((& $read).Length -gt 0) {
    "② 试 Escape"
    [void][Zc.N]::PostMessage($top, 0x0100, [IntPtr]0x1B, [IntPtr]0x00010001)
    [void][Zc.N]::PostMessage($top, 0x0101, [IntPtr]0x1B, [IntPtr]0xC0010001)
    Start-Sleep -Milliseconds 600
    "Escape 后: '$(& $read)'"
}
if ((& $read).Length -gt 0) { "❌ 清不掉，需要人工在有桌面的情况下 Ctrl+A / Delete" ; exit 1 }
"✅ 已清空"; exit 0
