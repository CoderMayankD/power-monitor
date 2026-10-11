# Power Monitor window for Windows: current draw, per-component breakdown, units used in the last N hours.
param([string]$LogDir = (Join-Path $env:ProgramData 'PowerMonitor\logs'))

Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Windows.Forms.DataVisualization
[System.Windows.Forms.Application]::EnableVisualStyles()

$RefreshS = 10
$TableRows = 300
$StaleS = 60
$GapS = 35
$Inv = [System.Globalization.CultureInfo]::InvariantCulture
$Components = @('cpu', 'gpu', 'ram', 'ssd', 'board', 'fans', 'network', 'other', 'psu_loss')
$Labels = @{ cpu = 'CPU'; gpu = 'GPU'; ram = 'RAM'; ssd = 'SSDs'; board = 'Motherboard'; fans = 'Fans / cooling'
             network = 'Network'; other = 'USB / other'; psu_loss = 'PSU loss' }
$Columns = @($Components | ForEach-Object { "${_}_w" }) + @('est_total_w')
$TOTAL = $Components.Count

# ------------------------------------------------------------------ incremental log reader
# Each row: @([datetime] timestamp, [double] interval_s, [double[]] values ordered like $Columns)
$script:Files = @{}

function Read-Rows([datetime]$since) {
    $firstDay = $since.ToString('yyyy-MM-dd')
    $out = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $LogDir)) { return , $out }
    foreach ($f in Get-ChildItem -LiteralPath $LogDir -Filter '????-??-??.csv' | Sort-Object Name) {
        if ($f.BaseName -lt $firstDay) { continue }
        $ent = $script:Files[$f.FullName]
        if (-not $ent -or $f.Length -lt $ent.Offset) {
            $ent = @{ Offset = [long]0; Index = $null; Rows = (New-Object System.Collections.Generic.List[object]) }
            $script:Files[$f.FullName] = $ent
        }
        if ($f.Length -gt $ent.Offset) {
            try {
                $fs = [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
                try {
                    [void]$fs.Seek($ent.Offset, 'Begin')
                    $buf = New-Object byte[] ($fs.Length - $ent.Offset)
                    $n = $fs.Read($buf, 0, $buf.Length)
                } finally { $fs.Close() }
            } catch { $n = 0 }
            $end = 0
            if ($n -gt 0) { $end = [Array]::LastIndexOf($buf, [byte]10, $n - 1) + 1 }  # skip a half-written last line
            if ($end -gt 0) {
                $text = [System.Text.Encoding]::UTF8.GetString($buf, 0, $end)
                foreach ($line in $text.Split("`n")) {
                    $p = $line.Trim().Split(',')
                    if ($p.Count -lt 3) { continue }
                    if ($null -eq $ent.Index) {
                        $ent.Index = @($Columns | ForEach-Object { [array]::IndexOf($p, $_) })
                        $ent.Ts = [array]::IndexOf($p, 'timestamp'); $ent.Iv = [array]::IndexOf($p, 'interval_s')
                        continue
                    }
                    try {
                        $vals = New-Object double[] $Columns.Count
                        for ($k = 0; $k -lt $Columns.Count; $k++) {
                            $i = $ent.Index[$k]
                            if ($i -ge 0) { $vals[$k] = [double]::Parse($p[$i], $Inv) }
                        }
                        $ts = [datetime]::ParseExact($p[$ent.Ts], "yyyy-MM-dd'T'HH:mm:ss", $Inv)
                        $ent.Rows.Add(@($ts, [double]::Parse($p[$ent.Iv], $Inv), $vals))
                    } catch { }
                }
                $ent.Offset += $end
            }
        }
        foreach ($r in $ent.Rows) { if ($r[0] -ge $since) { $out.Add($r) } }
    }
    return , $out
}

function Read-Hardware {
    $hw = @{}
    try {
        $doc = Get-Content -Raw -LiteralPath (Join-Path $LogDir 'hardware.json') | ConvertFrom-Json
        foreach ($c in $doc.components) { $hw[$c.key] = $c }
    } catch { }
    return $hw
}

function Format-Age([double]$s) {
    if ($s -lt 90) { return '{0:0} s' -f $s }
    if ($s -lt 5400) { return '{0:0} min' -f ($s / 60) }
    return '{0:0.0} h' -f ($s / 3600)
}

# ------------------------------------------------------------------ window

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Power Monitor'
$form.Size = New-Object System.Drawing.Size(760, 760)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

$layout = New-Object System.Windows.Forms.TableLayoutPanel
$layout.Dock = 'Fill'; $layout.Padding = 10; $layout.ColumnCount = 1; $layout.RowCount = 3
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle('AutoSize')))
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle('Percent', 100)))
[void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle('AutoSize')))
$form.Controls.Add($layout)

$head = New-Object System.Windows.Forms.FlowLayoutPanel
$head.FlowDirection = 'TopDown'; $head.AutoSize = $true; $head.WrapContents = $false; $head.Dock = 'Fill'
$layout.Controls.Add($head, 0, 0)

function New-Label([float]$size = 9, [string]$style = 'Regular') {
    $l = New-Object System.Windows.Forms.Label
    $l.AutoSize = $true
    $l.Font = New-Object System.Drawing.Font('Segoe UI', $size, [System.Drawing.FontStyle]$style)
    $head.Controls.Add($l)
    return $l
}
$nowLbl = New-Label 20 'Bold'
$partsLbl = New-Label
$statusLbl = New-Label

$ctl = New-Object System.Windows.Forms.FlowLayoutPanel
$ctl.AutoSize = $true; $ctl.Margin = '0,8,0,4'
function Add-Ctl($c) { $c.Margin = '0,4,6,0'; $ctl.Controls.Add($c) }
$t = New-Object System.Windows.Forms.Label; $t.Text = 'Last'; $t.AutoSize = $true; Add-Ctl $t
$hours = New-Object System.Windows.Forms.NumericUpDown
$hours.Minimum = 1; $hours.Maximum = 720; $hours.Value = 24; $hours.Width = 60; Add-Ctl $hours
$t = New-Object System.Windows.Forms.Label; $t.Text = 'hours          Price / unit'; $t.AutoSize = $true; Add-Ctl $t
$price = New-Object System.Windows.Forms.NumericUpDown
$price.Minimum = 0; $price.Maximum = 1000; $price.DecimalPlaces = 2; $price.Increment = 0.5; $price.Width = 70; Add-Ctl $price
$head.Controls.Add($ctl)

$unitsLbl = New-Label 12 'Bold'
$statsLbl = New-Label

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$layout.Controls.Add($tabs, 0, 1)

function New-ListView([string[]]$cols, [int[]]$widths) {
    $lv = New-Object System.Windows.Forms.ListView
    $lv.View = 'Details'; $lv.FullRowSelect = $true; $lv.Dock = 'Fill'; $lv.ShowItemToolTips = $true
    for ($i = 0; $i -lt $cols.Count; $i++) {
        $align = if ($i -ge 2 -or ($cols.Count -gt 6 -and $i -ge 1)) { 'Right' } else { 'Left' }
        [void]$lv.Columns.Add($cols[$i], $widths[$i], $align)
    }
    return $lv
}

$breakdown = New-ListView @('Component', 'Hardware', 'Now W', 'Avg W', 'kWh', 'Share') @(120, 330, 60, 60, 65, 55)
$page = New-Object System.Windows.Forms.TabPage 'Breakdown'; $page.Controls.Add($breakdown); $tabs.TabPages.Add($page)

$chart = New-Object System.Windows.Forms.DataVisualization.Charting.Chart
$chart.Dock = 'Fill'
$area = New-Object System.Windows.Forms.DataVisualization.Charting.ChartArea
$area.AxisX.LabelStyle.Format = 'dd MMM HH:mm'
$area.AxisX.MajorGrid.LineColor = [System.Drawing.Color]::Gainsboro
$area.AxisY.MajorGrid.LineColor = [System.Drawing.Color]::Gainsboro
$area.AxisY.Title = 'W'
$chart.ChartAreas.Add($area)
$legend = New-Object System.Windows.Forms.DataVisualization.Charting.Legend
$legend.Docking = 'Top'
$chart.Legends.Add($legend)
$seriesDefs = @(
    @('Total', [System.Drawing.Color]::FromArgb(66, 133, 245)),
    @('CPU', [System.Drawing.Color]::FromArgb(242, 140, 38)),
    @('GPU', [System.Drawing.Color]::FromArgb(77, 191, 102)),
    @('Other parts', [System.Drawing.Color]::FromArgb(166, 115, 217))
)
foreach ($d in $seriesDefs) {
    $s = New-Object System.Windows.Forms.DataVisualization.Charting.Series $d[0]
    $s.ChartType = 'Line'; $s.Color = $d[1]; $s.BorderWidth = 2; $s.XValueType = 'DateTime'
    $s.EmptyPointStyle.Color = [System.Drawing.Color]::Transparent
    $chart.Series.Add($s)
}
$page = New-Object System.Windows.Forms.TabPage 'Chart'; $page.Controls.Add($chart); $tabs.TabPages.Add($page)

$logCols = @('Time') + @($Components | ForEach-Object { $Labels[$_] }) + @('Total')
$logView = New-ListView $logCols (@(130) + @(70) * ($logCols.Count - 1))
$page = New-Object System.Windows.Forms.TabPage 'Log (W)'; $page.Controls.Add($logView); $tabs.TabPages.Add($page)

$note = New-Object System.Windows.Forms.Label
$note.AutoSize = $true
$note.Text = "Hover a component for how its power is measured or estimated.  Logs: $LogDir"
$layout.Controls.Add($note, 0, 2)

# ------------------------------------------------------------------ refresh

function Update-Chart($rows, [datetime]$since, [datetime]$until) {
    foreach ($s in $chart.Series) { $s.Points.Clear() }
    $area.AxisX.Minimum = $since.ToOADate(); $area.AxisX.Maximum = $until.ToOADate()
    if ($rows.Count -eq 0) { return }
    $n = 600
    $span = ($until - $since).TotalSeconds
    $sums = New-Object 'double[,]' $n, 4
    $counts = New-Object int[] $n
    foreach ($r in $rows) {
        $x = [int][math]::Floor(($r[0] - $since).TotalSeconds / $span * $n)
        $x = [math]::Max(0, [math]::Min($n - 1, $x))
        $v = $r[2]
        $counts[$x]++
        $sums[$x, 0] += $v[$TOTAL]; $sums[$x, 1] += $v[0]; $sums[$x, 2] += $v[1]
        $sums[$x, 3] += $v[$TOTAL] - $v[0] - $v[1]
    }
    $maxGap = [math]::Max(1, $GapS / $span * $n)
    $last = $null
    for ($x = 0; $x -lt $n; $x++) {
        if ($counts[$x] -eq 0) { continue }
        $t = $since.AddSeconds(($x + 0.5) * $span / $n).ToOADate()
        for ($k = 0; $k -lt 4; $k++) {
            $pts = $chart.Series[$k].Points
            if ($null -ne $last -and ($x - $last) -gt $maxGap) {  # break the line over a real gap
                $i = $pts.AddXY($t, 0); $pts[$i].IsEmpty = $true
            }
            [void]$pts.AddXY($t, $sums[$x, $k] / $counts[$x])
        }
        $last = $x
    }
}

function Update-View {
    $h = [double]$hours.Value
    $until = Get-Date
    $since = $until.AddHours(-$h)
    $rows = Read-Rows $since
    $p = [double]$price.Value
    $hw = Read-Hardware

    $breakdown.BeginUpdate(); $breakdown.Items.Clear()
    $logView.BeginUpdate(); $logView.Items.Clear()
    try {
        if ($rows.Count -eq 0) {
            $nowLbl.Text = '- W'
            $partsLbl.Text = ''
            $statusLbl.ForeColor = [System.Drawing.Color]::Firebrick
            $statusLbl.Text = "No data in the last $h h. Is the PowerMonitor scheduled task running?"
            $unitsLbl.Text = '0 units (kWh)'
            $statsLbl.Text = ''
            Update-Chart $rows $since $until
            return
        }
        $lastRow = $rows[$rows.Count - 1]
        $nv = $lastRow[2]
        $age = ($until - $lastRow[0]).TotalSeconds
        $nowLbl.Text = '{0:0} W  now (estimated total at the wall)' -f $nv[$TOTAL]
        $partsLbl.Text = 'CPU {0:0.0} W   ·   GPU {1:0.0} W   ·   rest of system {2:0.0} W' -f $nv[0], $nv[1], ($nv[$TOTAL] - $nv[0] - $nv[1])
        if ($age -gt $StaleS) {
            $statusLbl.ForeColor = [System.Drawing.Color]::Firebrick; $statusLbl.Text = "Logger looks stopped: last sample $(Format-Age $age) ago"
        } else {
            $statusLbl.ForeColor = [System.Drawing.Color]::ForestGreen; $statusLbl.Text = "● Logging   last sample $(Format-Age $age) ago"
        }

        $secs = 0.0
        $joules = New-Object double[] $Columns.Count
        $peak = 0.0
        foreach ($r in $rows) {
            $i = $r[1]; $v = $r[2]; $secs += $i
            for ($k = 0; $k -lt $Columns.Count; $k++) { $joules[$k] += $v[$k] * $i }
            if ($v[$TOTAL] -gt $peak) { $peak = $v[$TOTAL] }
        }
        $kwh = $joules[$TOTAL] / 3.6e6
        $avg = $joules[$TOTAL] / $secs
        $cost = if ($p -gt 0) { '   ·   cost {0:0.00}' -f ($kwh * $p) } else { '' }
        $unitsLbl.Text = '{0:0.000} units (kWh) in the last {1} h{2}' -f $kwh, $h, $cost
        $monthCost = if ($p -gt 0) { ' (cost {0:0})' -f ($avg * 0.72 * $p) } else { '' }
        $statsLbl.Text = ("avg {0:0} W   ·   peak {1:0} W   ·   logged {2:0.0} h`n" +
                          "At this average: {3:0.00} kWh/day, {4:0} kWh/30 days{5}") -f
                          $avg, $peak, ($secs / 3600), ($avg * 0.024), ($avg * 0.72), $monthCost

        for ($k = 0; $k -le $Components.Count; $k++) {
            if ($k -lt $Components.Count) {
                $c = $Components[$k]; $info = $hw[$c]
                $measured = $info -and "$($info.source)".StartsWith('Measured')
                $name = $Labels[$c] + $(if ($measured) { '' } else { ' (est.)' })
                $hwText = if ($info) { $info.hardware } else { '' }
                $tip = if ($info) { $info.source } else { '' }
            } else {
                $name = 'Total'; $hwText = 'Estimated power drawn from the wall socket'; $tip = ''
            }
            $item = New-Object System.Windows.Forms.ListViewItem $name
            [void]$item.SubItems.Add($hwText)
            [void]$item.SubItems.Add(('{0:0.0}' -f $nv[$k]))
            [void]$item.SubItems.Add(('{0:0.0}' -f ($joules[$k] / $secs)))
            [void]$item.SubItems.Add(('{0:0.000}' -f ($joules[$k] / 3.6e6)))
            [void]$item.SubItems.Add(('{0:0%}' -f ($joules[$k] / $joules[$TOTAL])))
            $item.ToolTipText = "$hwText`n$tip"
            if ($k -eq $Components.Count) { $item.Font = New-Object System.Drawing.Font($breakdown.Font, 'Bold') }
            [void]$breakdown.Items.Add($item)
        }

        $start = [math]::Max(0, $rows.Count - $TableRows)
        for ($j = $rows.Count - 1; $j -ge $start; $j--) {
            $r = $rows[$j]
            $item = New-Object System.Windows.Forms.ListViewItem ($r[0].ToString('dd MMM HH:mm:ss'))
            foreach ($v in $r[2]) { [void]$item.SubItems.Add(('{0:0.0}' -f $v)) }
            [void]$logView.Items.Add($item)
        }
        Update-Chart $rows $since $until
    } finally {
        $breakdown.EndUpdate(); $logView.EndUpdate()
    }
}

$hours.Add_ValueChanged({ Update-View })
$price.Add_ValueChanged({ Update-View })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $RefreshS * 1000
$timer.Add_Tick({ Update-View })
$form.Add_Shown({ Update-View; $timer.Start(); $tabs.Focus() })
[void]$form.ShowDialog()
$timer.Dispose()
