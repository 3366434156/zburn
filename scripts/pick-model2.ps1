param(
  [string]$ProcessName = 'ZCode',
  [string]$Wanted = 'zcode2api'
)

# 把当前对话的模型切到名字里带 $Wanted 的那一项。
# 模型选择器是 Radix 下拉：触发按钮只支持 ExpandCollapse（不支持 Invoke）；
# 列表项是 Text，真正能点的往往是上层容器，所以顺着父级往上找能 Select/Invoke 的那层。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("找不到 $ProcessName"); exit 2 }
$top = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
function Get-All { $top.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) }

$btn = $null
foreach ($e in (Get-All)) {
    if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Button' -and $e.Current.Name -match 'magpie|GLM') { $btn = $e; break }
}
if (-not $btn) { [Console]::Error.WriteLine('没找到模型选择按钮'); exit 3 }
Write-Output ("model button: '" + $btn.Current.Name + "'")
if ($btn.Current.Name -match [regex]::Escape($Wanted)) { Write-Output '已经是目标模型'; exit 0 }

try { $btn.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand() }
catch { [Console]::Error.WriteLine("展不开：" + $_.Exception.Message); exit 4 }
Start-Sleep -Milliseconds 1200

$target = $null
foreach ($e in (Get-All)) {
    $n = $e.Current.Name
    if ($n -and $n -match ("^" + [regex]::Escape($Wanted) + "/")) { $target = $e; break }
}
if (-not $target) {
    foreach ($e in (Get-All)) {
        $n = $e.Current.Name
        if ($n -and ($n -match [regex]::Escape($Wanted)) -and ($n -match 'GLM|Flash')) { $target = $e; break }
    }
}
if (-not $target) { [Console]::Error.WriteLine("列表里没有 '" + $Wanted + "' 这一项"); exit 5 }
Write-Output ("目标项：'" + $target.Current.Name + "'")

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$node = $target
$picked = $false
for ($depth = 0; $depth -lt 5 -and -not $picked; $depth++) {
    foreach ($pat in $node.GetSupportedPatterns()) {
        try {
            if ($pat.ProgrammaticName -eq 'SelectionItemPatternIdentifiers.Pattern') {
                $node.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); $picked = $true
            } elseif ($pat.ProgrammaticName -eq 'InvokePatternIdentifiers.Pattern') {
                $node.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $picked = $true
            } elseif ($pat.ProgrammaticName -eq 'LegacyIAccessiblePatternIdentifiers.Pattern') {
                $node.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern).DoDefaultAction(); $picked = $true
            }
        } catch { }
        if ($picked) { Write-Output ("用 " + $pat.ProgrammaticName + "（深度 " + $depth + "）选中"); break }
    }
    if (-not $picked) {
        try { $node = $walker.GetParent($node) } catch { break }
        if (-not $node) { break }
    }
}
if (-not $picked) { [Console]::Error.WriteLine('找不到可点击的层级'); exit 6 }
Start-Sleep -Milliseconds 1200

$now = $null
foreach ($e in (Get-All)) {
    if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Button' -and $e.Current.Name -match 'magpie|GLM') { $now = $e.Current.Name; break }
}
Write-Output ("当前模型按钮：" + $now)
if ($now -match [regex]::Escape($Wanted)) { Write-Output 'OK'; exit 0 }
[Console]::Error.WriteLine('切换后仍不是目标模型')
exit 7
