# Restart local quantbot service (ASCII only - PowerShell 5.1 reads files as ANSI)
Write-Host "=== kill uvicorn ==="
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -like '*uvicorn*' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Host ("  kill PID " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "  no uvicorn running"
}
Start-Sleep -Seconds 2

Write-Host "=== start ==="
Start-Process wscript.exe -ArgumentList 'E:\quant-bot\run_hidden.vbs'
Write-Host "  launched, waiting for port..."

$ok = $false
for ($i = 1; $i -le 45; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8899/api/status" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            Write-Host ("  port ready after " + ($i*2) + "s")
            $ok = $true
            break
        }
    } catch { }
}
if (-not $ok) { Write-Host "  !! port not ready in 90s" }
Write-Host "DONE"
