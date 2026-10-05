# 平日朝6:00にタスクスケジューラ(VideoAnalyzerWorker_Stop)から呼ばれる停止スクリプト。
# run_worker_loop.ps1(supervisor)ごと止めないと、supervisorがpython終了を検知して
# 30秒後に再起動してしまうため、supervisor(powershell)とpython両方を止める。

$ErrorActionPreference = "Continue"

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$supervisorLog = Join-Path $logDir "worker_supervisor.log"

function Write-SupervisorLog($message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Write-Output $line
    Add-Content -Path $supervisorLog -Value $line -Encoding utf8
}

Write-SupervisorLog "=== 平日朝の自動停止(06:00)開始 ==="

# run_worker_loop.ps1 を実行しているpowershellプロセス(supervisor)を止める
$supervisors = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'run_worker_loop\.ps1' }
foreach ($p in $supervisors) {
    Write-SupervisorLog "supervisor停止: PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# backfill.py / worker.py を実行しているpythonプロセスを止める
$workers = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'backfill\.py|worker\.py' }
foreach ($p in $workers) {
    Write-SupervisorLog "worker(python)停止: PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-SupervisorLog "=== 平日朝の自動停止 完了 ==="
