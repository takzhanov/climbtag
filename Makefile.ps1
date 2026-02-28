param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "venv", "install", "run", "run-bg", "stop", "restart", "status", "test", "benchmark-short", "benchmark-short-baseline", "benchmark-long", "clean", "docker-build", "docker-up", "docker-down")]
    [string]$Task = "help"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$VenvDir = ".venv"
$PythonExe = Join-Path $VenvDir "Scripts/python.exe"
$PipExe = Join-Path $VenvDir "Scripts/pip.exe"
$App = "app.main:app"
$HostAddr = "127.0.0.1"
$Port = "8888"

function Ensure-Venv {
    if (-not (Test-Path $PythonExe)) {
        python -m venv $VenvDir
    }
}

function Show-Help {
    Write-Host "Usage: .\Makefile.ps1 <task>"
    Write-Host ""
    Write-Host "Tasks:"
    Write-Host "  help          Show this help"
    Write-Host "  venv          Create .venv"
    Write-Host "  install       Create .venv and install dependencies"
    Write-Host "  run           Start uvicorn (reload) in foreground on http://localhost:8888"
    Write-Host "  run-bg        Start uvicorn (reload) in background and write logs to logs/"
    Write-Host "  stop          Stop all project uvicorn processes"
    Write-Host "  restart       Stop and start uvicorn in background"
    Write-Host "  status        Show project uvicorn processes"
    Write-Host "  test          Run pytest"
    Write-Host "  benchmark-short           Run benchmark on short test video"
    Write-Host "  benchmark-short-baseline  Rebuild baseline for short test video"
    Write-Host "  benchmark-long            Run benchmark on 95min test video"
    Write-Host "  clean         Clear runtime data (input/videos, outputs/converted, state.json)"
    Write-Host "  docker-build  docker compose build"
    Write-Host "  docker-up     docker compose up -d"
    Write-Host "  docker-down   docker compose down"
}

function Get-UvicornProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "python" -and
        $_.CommandLine -match "uvicorn" -and
        $_.CommandLine -match "app.main:app"
    }
}

function Get-ChildProcessTreeIds {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$RootIds
    )
    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
    $byParent = @{}
    foreach ($proc in $all) {
        $parent = [int]$proc.ParentProcessId
        if (-not $byParent.ContainsKey($parent)) {
            $byParent[$parent] = @()
        }
        $byParent[$parent] += [int]$proc.ProcessId
    }

    $queue = New-Object System.Collections.Generic.Queue[int]
    $result = New-Object System.Collections.Generic.HashSet[int]
    foreach ($id in $RootIds) {
        if ($result.Add($id)) {
            $queue.Enqueue($id)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($byParent.ContainsKey($current)) {
            foreach ($child in $byParent[$current]) {
                if ($result.Add($child)) {
                    $queue.Enqueue($child)
                }
            }
        }
    }
    return @($result)
}

function Stop-Uvicorn {
    $procs = Get-UvicornProcesses
    if (-not $procs) {
        Write-Host "No project uvicorn processes found."
        return
    }
    $rootIds = @($procs | ForEach-Object { [int]$_.ProcessId })
    $idsToStop = Get-ChildProcessTreeIds -RootIds $rootIds
    foreach ($procId in ($idsToStop | Sort-Object -Descending)) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    Write-Host ("Stopped processes: {0}" -f (($idsToStop | Sort-Object) -join ", "))
}

function Start-UvicornBackground {
    Ensure-Venv
    $logsDir = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    $stdoutLog = Join-Path $logsDir "uvicorn.out.log"
    $stderrLog = Join-Path $logsDir "uvicorn.err.log"
    Start-Process `
        -FilePath (Join-Path $ProjectRoot $PythonExe) `
        -ArgumentList @("-m", "uvicorn", $App, "--reload", "--host", $HostAddr, "--port", $Port) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden

    Start-Sleep -Seconds 2
    Write-Host "Uvicorn started in background: http://localhost:$Port"
    Write-Host "Logs: $stdoutLog, $stderrLog"
}

switch ($Task) {
    "help" {
        Show-Help
    }
    "venv" {
        Ensure-Venv
        Write-Host "Virtual environment is ready: $VenvDir"
    }
    "install" {
        Ensure-Venv
        & $PythonExe -m pip install --upgrade pip
        & $PipExe install -r requirements.txt
    }
    "run" {
        Ensure-Venv
        & $PythonExe -m uvicorn $App --reload --host $HostAddr --port $Port
    }
    "run-bg" {
        Start-UvicornBackground
    }
    "stop" {
        Stop-Uvicorn
    }
    "restart" {
        Stop-Uvicorn
        Start-UvicornBackground
    }
    "status" {
        $procs = Get-UvicornProcesses
        if (-not $procs) {
            Write-Host "No project uvicorn processes found."
        } else {
            $procs | Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
        }
    }
    "test" {
        Ensure-Venv
        & $PythonExe -m pytest -q
    }
    "benchmark-short" {
        Ensure-Venv
        & $PythonExe "scripts/analysis_benchmark.py" --case short
    }
    "benchmark-short-baseline" {
        Ensure-Venv
        & $PythonExe "scripts/analysis_benchmark.py" --case short --write-baseline
    }
    "benchmark-long" {
        Ensure-Venv
        & $PythonExe "scripts/analysis_benchmark.py" --case long
    }
    "clean" {
        if (Test-Path "input/videos") {
            Get-ChildItem "input/videos" -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
        }
        if (Test-Path "outputs/converted") {
            Get-ChildItem "outputs/converted" -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
        }
        if (Test-Path "state.json") {
            Remove-Item "state.json" -Force
        }
    }
    "docker-build" {
        docker compose build
    }
    "docker-up" {
        docker compose up -d
    }
    "docker-down" {
        docker compose down
    }
}
