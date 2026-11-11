# Stop local quantbot service (ASCII only - PowerShell 5.1 reads files as ANSI)
# Only kills the python process running uvicorn, leaves other python alone.
Write-Host "=== stop local quantbot (uvicorn) ==="
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*uvicorn*' -and $_.CommandLine -like '*8899*' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Host ("  kill PID " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "  no uvicorn on 8899 found"
}
Start-Sleep -Seconds 3

# verify port is closed
$still = $false
for ($i = 1; $i -le 10; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8899/api/status" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $still = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($still) {
    Write-Host "  !! port 8899 still responding - something is still running"
} else {
    Write-Host "  port 8899 closed - local service stopped"
}
Write-Host "DONE"
