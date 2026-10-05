# Supervisor loop: runs backfill.py --start-worker-after, and restarts it
# automatically if it exits for any reason (crash, Windows Update reboot, etc).
# Registered in Task Scheduler ("VideoAnalyzerWorker") to start at:
#   - logon (any time - for recovery from unexpected reboots during the
#     operating window below)
#   - daily 18:00 (normal nightly start)
# A separate task "VideoAnalyzerWorker_Stop" stops this on weekday mornings
# at 06:00 (stop_worker.ps1). Weekends are left running continuously.
#
# Because the logon trigger fires at ANY time (Task Scheduler triggers don't
# know about the operating-hours schedule), this script itself checks
# whether "now" is inside the allowed operating window before starting
# anything. If a reboot happens during weekday daytime (06:00-18:00, when
# the PC is meant to be free for other work) and logon-autostarts this task,
# it will just exit immediately instead of kicking off analysis.
#
# Allowed window: weekdays 18:00-06:00(next day), weekends always.
#
# backfill.py skips already-processed videos, so it is safe to restart from
# scratch at any point - it will quickly catch up and hand off to worker.py.

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

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

function Test-WithinOperatingWindow {
    $now = Get-Date
    $dow = $now.DayOfWeek
    if ($dow -eq [DayOfWeek]::Saturday -or $dow -eq [DayOfWeek]::Sunday) {
        return $true
    }
    # Weekday: allowed 18:00-23:59:59 or 00:00-05:59:59
    return ($now.Hour -ge 18 -or $now.Hour -lt 6)
}

Write-SupervisorLog "=== backfill+worker supervisor started ==="

if (-not (Test-WithinOperatingWindow)) {
    Write-SupervisorLog "現在は稼働時間外(平日06:00-18:00)のため、起動せず終了します。次は18:00の定期トリガーで起動します。"
    exit 0
}

while ($true) {
    if (-not (Test-WithinOperatingWindow)) {
        Write-SupervisorLog "稼働時間外になったため、ループを終了します。"
        break
    }
    # 固定日付だと日が経つほどスキャン対象(=既処理チェック対象)が際限なく
    # 増えていくため、毎回「今日から1か月前」を動的に計算して--sinceに渡す。
    $sinceDate = (Get-Date).AddMonths(-1).ToString('yyyyMMdd')
    Write-SupervisorLog "Starting: backfill.py --start-worker-after (--since $sinceDate)"
    & "$PSScriptRoot\.venv\Scripts\python.exe" -u "$PSScriptRoot\backfill.py" `
        --since $sinceDate --channels ch1,ch4,ch6,ch2,ch3,ch10,ch5 --start-worker-after
    $exitCode = $LASTEXITCODE
    Write-SupervisorLog "Process exited (exit code: $exitCode). Restarting in 30 seconds."
    Start-Sleep -Seconds 30
}
