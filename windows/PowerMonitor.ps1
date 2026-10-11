# Power Monitor logger for Windows. Install.ps1 runs it at startup as a SYSTEM scheduled task.
# Writes one CSV per day, plus daily_summary.csv and hardware.json, to $LogDir.
# CPU is measured when Windows exposes an Energy Meter (RAPL) counter, GPU when nvidia-smi exists;
# everything else is estimated from the detected hardware and its live activity.
param([string]$LogDir = (Join-Path $env:ProgramData 'PowerMonitor\logs'))

$ErrorActionPreference = 'Continue'
$IntervalS = 10
$Inv = [System.Globalization.CultureInfo]::InvariantCulture

# ---- Estimates for parts without a power sensor. Edit to match your hardware. ----
$CpuEstW = @(10.0, 90.0)        # only if no CPU energy counter: idle W, extra W at 100% load
$GpuEstW = 10.0                 # only if no NVIDIA GPU: fixed W
$RamWPerDimm = @(2.5, 2.5)      # idle W, extra W at 100% CPU load
$NvmeW = @(1.5, 4.5)            # idle W, extra W at 100% disk busy
$SataSsdW = @(0.5, 2.5)
$HddW = @(4.0, 2.0)
$BoardW = 9.0                   # chipset, audio, LEDs, Super I/O
$VrmLoss = 0.10                 # CPU voltage regulator loss, fraction of CPU power
$FansW = 5.0                    # Windows exposes no fan sensors; assumes ~3 case fans + CPU cooler
$NetW = @{ ethernet = @(0.2, 0.8); wifi = @(0.2, 1.5) }   # link down W, link up W
$OtherW = 3.0                   # USB controllers and devices, misc
$PsuRatedW = 750                # assumed 80+ Gold PSU
$PsuCurve = @(@(0.0, 0.70), @(0.05, 0.80), @(0.10, 0.85), @(0.20, 0.89), @(0.50, 0.91), @(1.0, 0.88))

$Components = @('cpu', 'gpu', 'ram', 'ssd', 'board', 'fans', 'network', 'other', 'psu_loss')
$Labels = @{ cpu = 'CPU'; gpu = 'GPU'; ram = 'RAM'; ssd = 'SSDs'; board = 'Motherboard'; fans = 'Fans / cooling'
             network = 'Network'; other = 'USB / other'; psu_loss = 'PSU loss' }
$SampleHeader = (@('timestamp', 'interval_s') + ($Components | ForEach-Object { "${_}_w" }) + @('est_total_w')) -join ','
$SummaryHeader = (@('date', 'hours_logged', 'avg_w', 'peak_w') + ($Components | ForEach-Object { "${_}_kwh" }) +
                  @('est_total_kwh')) -join ','

function Fmt([double]$v, [string]$f = '0.00') { $v.ToString($f, $Inv) }

function Get-PsuEfficiency([double]$loadW) {
    $frac = $loadW / $PsuRatedW
    for ($i = 1; $i -lt $PsuCurve.Count; $i++) {
        $x0, $y0 = $PsuCurve[$i - 1]
        $x1, $y1 = $PsuCurve[$i]
        if ($frac -le $x1) { return $y0 + ($y1 - $y0) * ($frac - $x0) / ($x1 - $x0) }
    }
    return $PsuCurve[-1][1]
}

function Write-Atomic([string]$path, [string]$text) {
    $tmp = "$path.tmp"
    [System.IO.File]::WriteAllText($tmp, $text)
    Move-Item -LiteralPath $tmp -Destination $path -Force
}

# ------------------------------------------------------------------ hardware detection

function Get-NvidiaSmi {
    $cmd = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $p = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
    if (Test-Path $p) { return $p }
    return $null
}

function Get-CpuEnergyPaths {
    # Windows Energy Meter Interface exposes Intel/AMD RAPL as e.g. \Energy Meter(rapl_package0_pkg)\Power (mW)
    try {
        $set = Get-Counter -ListSet 'Energy Meter' -ErrorAction Stop
        return @($set.PathsWithInstances | Where-Object { $_ -match 'pkg\)\\Power$' })
    } catch { return @() }
}

function Get-Disks {
    $disks = @()
    try {
        foreach ($d in Get-PhysicalDisk -ErrorAction Stop) {
            $kind = if ("$($d.BusType)" -eq 'NVMe') { 'nvme' } elseif ("$($d.MediaType)" -eq 'HDD') { 'hdd' } else { 'ssd' }
            $disks += [pscustomobject]@{ Index = "$($d.DeviceId)"; Model = "$($d.FriendlyName)".Trim()
                                         SizeGB = [math]::Round($d.Size / 1e9); Kind = $kind }
        }
    } catch {
        foreach ($d in Get-CimInstance Win32_DiskDrive) {
            $disks += [pscustomobject]@{ Index = "$($d.Index)"; Model = "$($d.Model)".Trim()
                                         SizeGB = [math]::Round($d.Size / 1e9); Kind = 'ssd' }
        }
    }
    foreach ($d in $disks) {
        $w = switch ($d.Kind) { 'nvme' { $NvmeW } 'hdd' { $HddW } default { $SataSsdW } }
        $d | Add-Member -NotePropertyName Watts -NotePropertyValue $w
    }
    return $disks
}

function Get-Nics {
    $nics = @()
    foreach ($n in Get-NetAdapter -Physical -ErrorAction SilentlyContinue) {
        $wifi = "$($n.InterfaceDescription) $($n.PhysicalMediaType)" -match 'Wi-?Fi|Wireless|802\.11|WLAN'
        $nics += [pscustomobject]@{ Name = $n.Name; Model = $n.InterfaceDescription
                                    Kind = $(if ($wifi) { 'wifi' } else { 'ethernet' }) }
    }
    return $nics
}

function Get-Dimms {
    $types = @{ 20 = 'DDR'; 21 = 'DDR2'; 24 = 'DDR3'; 26 = 'DDR4'; 34 = 'DDR5'; 35 = 'LPDDR5' }
    $out = @()
    foreach ($m in Get-CimInstance Win32_PhysicalMemory) {
        $speed = if ($m.ConfiguredClockSpeed) { $m.ConfiguredClockSpeed } else { $m.Speed }
        $type = $types[[int]$m.SMBIOSMemoryType]
        $out += [pscustomobject]@{ SizeGB = [math]::Round($m.Capacity / 1GB); Type = $type; Speed = $speed
                                   Part = "$($m.PartNumber)".Trim() }
    }
    if (-not $out) { $out = @([pscustomobject]@{ SizeGB = 0; Type = ''; Speed = ''; Part = '' }) * 2 }
    return $out
}

# ------------------------------------------------------------------ setup

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$NvidiaSmi = Get-NvidiaSmi
$CpuEnergyPaths = Get-CpuEnergyPaths
$Disks = Get-Disks
$Nics = Get-Nics
$Dimms = Get-Dimms

function Write-Hardware {
    $cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name.Trim()
    $gpus = (Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) -join ', '
    $board = Get-CimInstance Win32_BaseBoard | Select-Object -First 1
    $ramGB = ($Dimms | Measure-Object SizeGB -Sum).Sum
    $ramDesc = "$ramGB GB " + (($Dimms | ForEach-Object { ("$($_.Type)-$($_.Speed) $($_.Part)").Trim() }) -join ', ') +
               " ($($Dimms.Count) DIMM)"
    $ssdIdle = ($Disks | ForEach-Object { $_.Watts[0] } | Measure-Object -Sum).Sum
    $info = [ordered]@{
        cpu = @($cpu, $(if ($CpuEnergyPaths) { 'Measured: Windows Energy Meter (RAPL package)' }
                        else { "Estimated from CPU load (no energy counter): $($CpuEstW[0]) W idle" }))
        gpu = @($gpus, $(if ($NvidiaSmi) { 'Measured: nvidia-smi (whole card)' }
                         else { "Fixed estimate: $GpuEstW W (no GPU power sensor)" }))
        ram = @($ramDesc, "Estimated: $($RamWPerDimm[0]) W/DIMM idle, up to +$($RamWPerDimm[1]) W/DIMM with CPU load")
        ssd = @((($Disks | ForEach-Object { "$($_.Model) $($_.SizeGB) GB" }) -join ', '),
                "Estimated from disk busy time: $(Fmt $ssdIdle '0.0') W idle total")
        board = @("$($board.Manufacturer) $($board.Product)".Trim(),
                  "Estimated: $BoardW W chipset/misc + $([int]($VrmLoss * 100))% of CPU power (VRM loss)")
        fans = @('Not visible (Windows has no fan sensor API)', "Fixed estimate: $FansW W")
        network = @((($Nics | ForEach-Object { "$($_.Model) ($($_.Name))" }) -join ', '), 'Estimated from link state (up/down)')
        other = @('USB controllers and devices, audio', "Fixed estimate: $OtherW W")
        psu_loss = @("Power supply (assumed $PsuRatedW W 80+ Gold)", 'Estimated from load using a typical efficiency curve')
    }
    $comps = foreach ($k in $Components) {
        [ordered]@{ key = $k; label = $Labels[$k]; hardware = $info[$k][0]; source = $info[$k][1] }
    }
    $doc = [ordered]@{ updated = (Get-Date).ToString('s'); components = @($comps) }
    Write-Atomic (Join-Path $LogDir 'hardware.json') ($doc | ConvertTo-Json -Depth 4)
}

function Write-Summary {
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add($SummaryHeader)
    foreach ($f in Get-ChildItem -Path $LogDir -Filter '????-??-??.csv' | Sort-Object Name) {
        $secs = 0.0; $totJ = 0.0; $peak = 0.0
        $compJ = @{}; foreach ($c in $Components) { $compJ[$c] = 0.0 }
        foreach ($r in Import-Csv -LiteralPath $f.FullName) {
            try {
                $i = [double]::Parse($r.interval_s, $Inv); $t = [double]::Parse($r.est_total_w, $Inv)
            } catch { continue }
            $secs += $i; $totJ += $t * $i; if ($t -gt $peak) { $peak = $t }
            foreach ($c in $Components) {
                $v = $r."${c}_w"
                if ($v) { $compJ[$c] += [double]::Parse($v, $Inv) * $i }
            }
        }
        if ($secs -le 0) { continue }
        $cols = @($f.BaseName, (Fmt ($secs / 3600)), (Fmt ($totJ / $secs) '0.0'), (Fmt $peak '0.0')) +
                ($Components | ForEach-Object { Fmt ($compJ[$_] / 3.6e6) '0.0000' }) + @(Fmt ($totJ / 3.6e6) '0.0000')
        $lines.Add($cols -join ',')
    }
    Write-Atomic (Join-Path $LogDir 'daily_summary.csv') (($lines -join "`r`n") + "`r`n")
}

function Get-Sample([double]$elapsed) {
    $paths = @('\Processor(_Total)\% Processor Time', '\PhysicalDisk(*)\% Idle Time') + $CpuEnergyPaths
    $samples = (Get-Counter -Counter $paths -ErrorAction SilentlyContinue).CounterSamples
    $cpuUtil = 0.0; $cpuMw = $null; $diskIdle = @{}
    foreach ($s in $samples) {
        if ($s.Path -match '\\processor\(_total\)\\') { $cpuUtil = [math]::Min(1.0, $s.CookedValue / 100) }
        elseif ($s.Path -match '\\physicaldisk\((\d+)[^)]*\)\\') { $diskIdle[$Matches[1]] = $s.CookedValue }
        elseif ($s.Path -match 'energy meter') { $cpuMw += $s.CookedValue }
    }

    $w = [ordered]@{}
    $cpuW = if ($cpuMw -ne $null) { $cpuMw / 1000 } else { $null }
    if ($cpuW -eq $null -or $cpuW -lt 0.1 -or $cpuW -gt 1000) { $cpuW = $CpuEstW[0] + $CpuEstW[1] * $cpuUtil }
    $w.cpu = $cpuW

    $gpuW = $null
    if ($NvidiaSmi) {
        $out = & $NvidiaSmi --query-gpu=power.draw --format=csv,noheader,nounits 2>$null
        foreach ($line in $out) {
            $v = 0.0
            if ([double]::TryParse("$line".Trim(), [System.Globalization.NumberStyles]::Float, $Inv, [ref]$v)) { $gpuW += $v }
        }
    }
    $w.gpu = if ($gpuW -ne $null) { $gpuW } else { $GpuEstW }

    $w.ram = $Dimms.Count * ($RamWPerDimm[0] + $RamWPerDimm[1] * $cpuUtil)
    $ssd = 0.0
    foreach ($d in $Disks) {
        $busy = 0.0
        if ($diskIdle.ContainsKey($d.Index)) { $busy = 1 - [math]::Min(100, [math]::Max(0, $diskIdle[$d.Index])) / 100 }
        $ssd += $d.Watts[0] + $d.Watts[1] * $busy
    }
    $w.ssd = $ssd
    $w.board = $BoardW + $VrmLoss * $w.cpu
    $w.fans = $FansW
    $net = 0.0
    $status = @{}
    foreach ($a in Get-NetAdapter -Physical -ErrorAction SilentlyContinue) { $status[$a.Name] = "$($a.Status)" }
    foreach ($n in $Nics) { $net += $NetW[$n.Kind][[int]($status[$n.Name] -eq 'Up')] }
    $w.network = $net
    $w.other = $OtherW
    $dc = 0.0; foreach ($v in $w.Values) { $dc += $v }
    $w.psu_loss = $dc / (Get-PsuEfficiency $dc) - $dc
    return $w
}

# ------------------------------------------------------------------ main loop

Write-Hardware
Write-Summary
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$prev = $clock.Elapsed.TotalSeconds
$currentDay = $null

while ($true) {
    $sleep = $IntervalS - (($clock.Elapsed.TotalSeconds - $prev) % $IntervalS)
    Start-Sleep -Milliseconds ([int]($sleep * 1000))
    try {
        $now = $clock.Elapsed.TotalSeconds
        $elapsed = $now - $prev
        $prev = $now
        $w = Get-Sample $elapsed
        $stamp = Get-Date
        $day = $stamp.ToString('yyyy-MM-dd')
        $path = Join-Path $LogDir "$day.csv"
        if ($day -ne $currentDay) {
            if ($currentDay) { Write-Summary }
            if (-not (Test-Path -LiteralPath $path)) { [System.IO.File]::WriteAllText($path, "$SampleHeader`r`n") }
            $currentDay = $day
        }
        $total = 0.0; foreach ($v in $w.Values) { $total += $v }
        $cols = @($stamp.ToString('yyyy-MM-ddTHH:mm:ss'), (Fmt $elapsed)) +
                ($Components | ForEach-Object { Fmt $w[$_] }) + @(Fmt $total)
        [System.IO.File]::AppendAllText($path, ($cols -join ',') + "`r`n")
    } catch {
        # Keep logging even if one reading fails.
        Start-Sleep -Seconds 1
    }
}
