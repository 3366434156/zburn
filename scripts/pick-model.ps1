param(
  [string]$ProcessName = 'ZCode',
  [string]$Wanted = 'zcode2api'
)

# 把当前对话的模型切到名字里带 $Wanted 的那一项。
#
# 模型选择器是 Radix 下拉：RadioButton 自报支持 SelectionItem/Invoke，
# 但 Select()/Invoke() 在 ZCode 里都是空操作 —— 菜单里那一项会变成「已选中」，
# 输入框旁的模型按钮却还是旧模型（实测切了 3 次仍是 auto/deepseek）。
# Radix 只认真实输入事件；SetCursorPos + mouse_event 又要求窗口已是前台
# （后台进程调 SetForegroundWindow 会被系统拒绝，第一下点击只用来激活窗口），
# 所以这里走「UIA SetFocus + 投递 Enter」：聚焦是 UIA 的合法操作，
# Enter 走 WM_KEYDOWN 直接进渲染进程，不需要前台焦点，和 WM_CHAR 投喂同一套路子。
# 选完必须把下拉关掉：菜单开着时 Radix 给整个应用挂 aria-hidden，
# 无障碍树里既没有输入框也没有侧栏会话 —— 投喂/扫描会全部停摆。

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -Namespace Zburn -Name ModelKey -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
"@

$WM_KEYDOWN = 0x0100; $WM_KEYUP = 0x0101
$VK_RETURN = 0x0D; $VK_SPACE = 0x20; $VK_ESCAPE = 0x1B
$AT = [System.Windows.Automation.AutomationElement]

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { [Console]::Error.WriteLine("找不到 $ProcessName"); exit 2 }
$h = $proc.MainWindowHandle
$top = $AT::FromHandle($h)
function Get-All { $top.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) }
function Test-MenuOpen { @(Get-All | Where-Object { $_.Current.ControlType.ProgrammaticName -eq 'ControlType.Menu' }).Count -gt 0 }

function Get-Trigger {
    foreach ($e in (Get-All)) {
        if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Button') { continue }
        if ($e.Current.ClassName -match 'composer-model-trigger') { return $e }
    }
    foreach ($e in (Get-All)) {
        if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.Button') { continue }
        $n = $e.Current.Name
        if ($n -and $n -match '^[^\\/]+/[^\\/]+' -and $n -match '最高|模型|model') { return $e }
    }
    return $null
}

# 下拉开着时 Esc 关掉；关不掉返回 $false
function Close-Menu {
    for ($i = 0; $i -lt 3 -and (Test-MenuOpen); $i++) {
        [void][Zburn.ModelKey]::PostMessage($h, $WM_KEYDOWN, [IntPtr]$VK_ESCAPE, [IntPtr]0x00010001)
        Start-Sleep -Milliseconds 90
        [void][Zburn.ModelKey]::PostMessage($h, $WM_KEYUP, [IntPtr]$VK_ESCAPE, [IntPtr]0xC0010001)
        Start-Sleep -Milliseconds 600
    }
    return -not (Test-MenuOpen)
}

function Send-Key([int]$vk) {
    [void][Zburn.ModelKey]::PostMessage($h, $WM_KEYDOWN, [IntPtr]$vk, [IntPtr]0x00010001)
    Start-Sleep -Milliseconds 90
    [void][Zburn.ModelKey]::PostMessage($h, $WM_KEYUP, [IntPtr]$vk, [IntPtr]0xC0010001)
}

$trigger = Get-Trigger
if (-not (Test-MenuOpen) -and $trigger -and $trigger.Current.Name -match [regex]::Escape($Wanted)) {
    Write-Output ("已经是目标模型：'" + $trigger.Current.Name + "'")
    exit 0
}
if ($trigger) { Write-Output ("model button: '" + $trigger.Current.Name + "'") }

if (-not (Test-MenuOpen)) {
    if (-not $trigger) { [Console]::Error.WriteLine('没找到模型选择按钮'); exit 3 }
    try { $trigger.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand() }
    catch { [Console]::Error.WriteLine("展不开：" + $_.Exception.Message); exit 4 }
    Start-Sleep -Milliseconds 1200
}

function Find-Target {
    foreach ($e in (Get-All)) {
        $n = $e.Current.Name
        if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.RadioButton' -and
            $n -and $n -match ("^" + [regex]::Escape($Wanted) + "/")) { return $e }
    }
    foreach ($e in (Get-All)) {
        $n = $e.Current.Name
        if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.RadioButton' -and
            $n -and ($n -match [regex]::Escape($Wanted))) { return $e }
    }
    return $null
}

$target = Find-Target
if (-not $target) {
    [void](Close-Menu)
    [Console]::Error.WriteLine("列表里没有 '" + $Wanted + "' 这一项"); exit 5
}
Write-Output ("目标项：'" + $target.Current.Name + "'")

# 菜单是滚动列表：目标项可能落在固定页脚（供应商/思考强度）下面，
# 直接按坐标点会点到滑块。先滚进可视区，再聚焦。
try { $target.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView() }
catch { }
Start-Sleep -Milliseconds 500
try { $target.SetFocus() }
catch { [void](Close-Menu); [Console]::Error.WriteLine('聚焦失败：' + $_.Exception.Message); exit 6 }
Start-Sleep -Milliseconds 250

$picked = $false
foreach ($vk in @($VK_RETURN, $VK_SPACE)) {
    Send-Key $vk
    Start-Sleep -Milliseconds 1000
    if (-not (Test-MenuOpen)) { $picked = $true; break }
    if ((Get-Trigger) -and (Get-Trigger).Current.Name -match [regex]::Escape($Wanted)) { $picked = $true; break }
}
if (Test-MenuOpen) { [void](Close-Menu) }
Start-Sleep -Milliseconds 400

$now = Get-Trigger
Write-Output ("当前模型按钮：" + $(if ($now) { $now.Current.Name } else { '(读不到)' }))
if ($now -and $now.Current.Name -match [regex]::Escape($Wanted)) { Write-Output 'OK'; exit 0 }
if ($picked) { [Console]::Error.WriteLine('已选中但模型按钮没更新'); exit 7 }
[Console]::Error.WriteLine('切换后仍不是目标模型')
exit 7
