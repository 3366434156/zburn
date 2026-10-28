param([string]$ProcessName = 'ZCode', [string]$SendButtonRegex = '发送|加入队列|立即|Send|发送消息', [string]$ExcludeButtonRegex = '移除待发送消息|停止生成|Remove queued|Stop generating')

# 把输入框里的残留内容「发出去」以腾空输入框。
# ZCode 2.x 上退格/Ctrl+A/ValuePattern.SetValue('') 全是空操作，依次尝试：
#   1) 离输入框最近的发送按钮 Invoke；2) LegacyIAccessible 默认动作；3) 给窗口发回车。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FlushNative {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}
"@

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("找不到 $ProcessName 主窗口"); exit 2 }
$top = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)

$all = $top.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
$edits = @(); $buttons = @()
foreach ($e in $all) {
    $ct = $e.Current.ControlType.ProgrammaticName
    if ($ct -eq 'ControlType.Edit') { $edits += $e }
    elseif ($ct -eq 'ControlType.Button' -and $e.Current.Name) { $buttons += $e }
}
if ($edits.Count -eq 0) { [Console]::Error.WriteLine('没找到输入框'); exit 3 }

$composer = $edits | Sort-Object { $_.Current.BoundingRectangle.Width * $_.Current.BoundingRectangle.Height } -Descending | Select-Object -First 1
$cr = $composer.Current.BoundingRectangle
$vp = $null
try { $vp = $composer.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern) } catch { }
$readText = { try { ($vp.Current.Value -replace "[`r`n]+$", '') } catch { '' } }
$text = & $readText
Write-Output ("composer len={0} '{1}'" -f $text.Length, $text.Substring(0, [Math]::Min(40, $text.Length)))
if ($text.Length -eq 0) { Write-Output '已经是空的'; exit 0 }

# 最近的发送按钮（不按 X 排序：ZCode 右侧还有内置浏览器的发送按钮，会选错）
$cand = @($buttons | Where-Object { $_.Current.Name -match $SendButtonRegex -and ($ExcludeButtonRegex -eq '' -or $_.Current.Name -notmatch $ExcludeButtonRegex) })
$send = $cand | Sort-Object {
    $r = $_.Current.BoundingRectangle
    [Math]::Abs($r.X - $cr.Right) + [Math]::Abs($r.Y - $cr.Y)
} | Select-Object -First 1
if ($send) {
    $sr = $send.Current.BoundingRectangle
    Write-Output ("send button '{0}' at ({1},{2})" -f $send.Current.Name, [int]$sr.X, [int]$sr.Y)
}

$submitted = $false
if ($send) {
    try { $send.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch { }
    Start-Sleep -Milliseconds 700
    $submitted = ((& $readText).Length -eq 0)
}
if (-not $submitted -and $send) {
    try {
        $leg = $send.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        $leg.DoDefaultAction()
        Start-Sleep -Milliseconds 700
        $submitted = ((& $readText).Length -eq 0)
    } catch { }
}
if (-not $submitted) {
    # 兜底：输入框还是焦点，往窗口发一个回车
    [void][FlushNative]::PostMessage($proc.MainWindowHandle, 0x0100, [IntPtr]0x0D, [IntPtr]0x001C0001)
    Start-Sleep -Milliseconds 80
    [void][FlushNative]::PostMessage($proc.MainWindowHandle, 0x0101, [IntPtr]0x0D, [IntPtr]0xC01C0001)
    Start-Sleep -Milliseconds 900
    $submitted = ((& $readText).Length -eq 0)
}

if ($submitted) { Write-Output '已发出，输入框清空'; exit 0 }
Write-Output ("发送后输入框仍有 {0} 字" -f (& $readText).Length)
exit 6

