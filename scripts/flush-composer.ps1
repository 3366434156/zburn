param([string]$ProcessName = 'ZCode', [string]$SendButtonRegex = '发送|加入队列|立即|Send', [string]$ExcludeButtonRegex = '移除待发送消息|停止生成|Remove queued|Stop generating')

# 把输入框里的残留内容「发出去」以腾空输入框。
# ZCode 2.x 上退格/Ctrl+A/ValuePattern.SetValue('') 全都是空操作，唯一可靠的办法：
# 找到发送按钮 Invoke 一次，让它当作一条普通消息发掉，输入框即恢复为空。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

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
$vp = $null
try { $vp = $composer.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern) } catch { }
$text = ''
if ($vp) { $text = ($vp.Current.Value -replace "[`r`n]+$", '') }
Write-Output ("composer len={0} '{1}'" -f $text.Length, $text.Substring(0, [Math]::Min(40, $text.Length)))
if ($text.Length -eq 0) { Write-Output '已经是空的'; exit 0 }

$btn = $buttons | Where-Object { $_.Current.Name -match $SendButtonRegex -and ($ExcludeButtonRegex -eq '' -or $_.Current.Name -notmatch $ExcludeButtonRegex) } | Select-Object -First 1
if (-not $btn) { [Console]::Error.WriteLine('没找到发送按钮'); exit 4 }
try {
    $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
} catch { [Console]::Error.WriteLine("发送失败：$($_.Exception.Message)"); exit 5 }

for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Milliseconds 400
    $now = '';
    if ($vp) { try { $now = ($vp.Current.Value -replace "[`r`n]+$", '') } catch { } }
    if ($now.Length -eq 0) { Write-Output '已发出，输入框清空'; exit 0 }
}
Write-Output '发送后输入框仍有内容'
exit 6
