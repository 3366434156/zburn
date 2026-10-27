# 诊断：列出目标窗口里所有 Edit 控件（含位置、大小、当前文本预览）
# 用来搞清楚「哪个才是聊天输入框」，以及为什么清空无效。
param([string]$ProcessName = 'ZCode', [int]$MaxDepth = 40)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $proc) { 'no window'; exit 1 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$wr = $root.Current.BoundingRectangle
"窗口: ({0},{1}) {2}x{3}" -f [int]$wr.X, [int]$wr.Y, [int]$wr.Width, [int]$wr.Height
$rows = New-Object System.Collections.ArrayList
function Walk($el, $d) {
    if ($d -gt $MaxDepth) { return }
    try {
        $ct = $el.Current.ControlType.ProgrammaticName
        if ($ct -eq 'ControlType.Edit') {
            $r = $el.Current.BoundingRectangle
            $txt = ''
            try {
                $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $txt = $vp.Current.Value
            } catch { $txt = '<no ValuePattern>' }
            $ro = $false
            try { $ro = $el.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsReadOnlyProperty) } catch { }
            [void]$rows.Add([pscustomobject]@{
                    d = $d; x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height
                    readonly = $ro; len = $txt.Length
                    preview = ($txt -replace "`r?`n", ' ').Substring(0, [Math]::Min(40, $txt.Length))
                })
        }
    } catch { }
    try { $c = $walker.GetFirstChild($el) } catch { return }
    while ($c) { Walk $c ($d + 1); try { $c = $walker.GetNextSibling($c) } catch { break } }
}
Walk $root 0
"共 {0} 个 Edit：" -f $rows.Count
foreach ($r in ($rows | Sort-Object y)) {
    "  d{0,-2} ({1},{2}) {3}x{4} readonly={5,-5} len={6,-4} '{7}'" -f $r.d, $r.x, $r.y, $r.w, $r.h, $r.readonly, $r.len, $r.preview
}
