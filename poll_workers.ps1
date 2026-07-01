param($codesDir, $statusFile)
"Started polling at $(Get-Date)" | Out-File $statusFile -Encoding utf8
for ($check = 1; $check -le 15; $check++) {
    Start-Sleep -Seconds 120
    $doneCount = 0
    $line = "Check $check at $(Get-Date -Format 'HH:mm:ss'):"
    for ($i = 0; $i -le 4; $i++) {
        $logFile = Join-Path $codesDir "reimport_revocation_w$i.log"
        $tail = Get-Content $logFile -Tail 5
        $done = $tail | Where-Object { $_ -match "Worker $i DONE" }
        $pg = $tail | Where-Object { $_ -match "Page \d+ \(\d+/\d+\)" } | Select-Object -Last 1
        if ($done) {
            $doneCount++
            $line += " w${i}=DONE"
        } elseif ($pg) {
            $pgInfo = [regex]::Match($pg, 'Page \d+ \(\d+/\d+\)').Value
            $line += " w${i}=$pgInfo"
        } else {
            $line += " w${i}=waiting"
        }
    }
    "$line [$doneCount/5]" | Add-Content $statusFile -Encoding utf8
    if ($doneCount -eq 5) {
        "ALL_WORKERS_DONE" | Add-Content $statusFile -Encoding utf8
        break
    }
}
"Poll loop ended at $(Get-Date)" | Add-Content $statusFile -Encoding utf8
