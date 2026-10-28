param([string]$ProcessName = 'ZCode')

# 读出当前对话用的模型名（ZCode 的输入框旁边那个按钮的文案，形如
# "magpie/auto/GLM-5.3-Flash 最高"）。只读，不动 UI。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("找不到 $ProcessName"); exit 2 }
$top = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)

$found = @()
foreach ($e in $top.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                            [System.Windows.Automation.Condition]::TrueCondition)) {
    if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Button') { continue }
    $n = $e.Current.Name
    if ($n -and $n -match 'magpie|GLM|模型|model') { $found += $n }
}
if (-not $found.Count) { [Console]::Error.WriteLine('没找到模型按钮'); exit 3 }
Write-Output ($found[0])
exit 0
