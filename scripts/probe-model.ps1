param([string]$ProcessName = 'ZCode')

# 读出当前对话用的模型名（ZCode 的输入框旁边那个按钮的文案，形如
# "magpie/auto/GLM-5.3-Flash 最高"）。只读，不动 UI。
#
# 不能只按 'model' 关键字找按钮：会话里改过的文件也会以按钮形式出现在
# 侧栏/正文（实测 probe-model 返回过 'pick-model.ps1'，模型校验因此永远失败）。
# 先认 class 里的 composer-model-trigger，再退回「含斜杠 + 最高/模型」的名字。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("找不到 $ProcessName"); exit 2 }
$top = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$all = $top.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    [System.Windows.Automation.Condition]::TrueCondition)

foreach ($e in $all) {
    if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Button' -and
        $e.Current.ClassName -match 'composer-model-trigger') {
        Write-Output $e.Current.Name
        exit 0
    }
}
foreach ($e in $all) {
    if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Button') { continue }
    $n = $e.Current.Name
    if ($n -and $n -match '^[^\\/]+/[^\\/]+' -and $n -match '最高|模型|model') {
        Write-Output $n
        exit 0
    }
}
[Console]::Error.WriteLine('没找到模型按钮'); exit 3
