# Stop the running translator.
# Usage: powershell -File kill_app.ps1 -Root "C:\path\to\translator"
param([string]$Root = "")

if (-not $Root) {
    $Root = (Get-Location).Path
}
$pidFile = Join-Path $Root 'logs\app.pid'
$killed = $false

if (Test-Path $pidFile) {
    $raw = (Get-Content $pidFile -Raw -ErrorAction SilentlyContinue)
    if ($raw) { $raw = $raw.Trim() }
    if ($raw -match '^[0-9]+$') {
        $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like 'python*') {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            Write-Host "  stopped translator (PID $($proc.Id))"
            $killed = $true
        }
    }
}

if (-not $killed) {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction Stop
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            if ($cmd -like '*app\main.py*' -or $cmd -like '*app/main.py*') {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Host "  stopped translator (PID $($p.ProcessId))"
                $killed = $true
            }
        }
    } catch {
    }
}

if (-not $killed) {
    Write-Host "  no running translator instance found"
}

Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
