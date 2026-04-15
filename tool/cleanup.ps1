$ErrorActionPreference = 'SilentlyContinue'

Write-Host "[cleanup] Starting Netskope environment cleanup..."

$processNames = @(
    'stAgentUI',
    'stAgentSvcMon',
    'stAgentSvc',
    'msiexec'
)

foreach ($processName in $processNames) {
    Get-Process -Name $processName | Stop-Process -Force
}

$serviceNames = @(
    'stwatchdog',
    'stAgentSvc',
    'stadrv'
)

foreach ($serviceName in $serviceNames) {
    sc.exe stop $serviceName | Out-Null
    sc.exe delete $serviceName | Out-Null
}

Start-Sleep -Seconds 3

$cleanupExecutables = @(
    'C:\Program Files (x86)\Netskope\STAgent\netskopecleanup.exe',
    'C:\Program Files\Netskope\STAgent\netskopecleanup.exe'
)

foreach ($cleanupExe in $cleanupExecutables) {
    if (Test-Path -Path $cleanupExe) {
        & $cleanupExe | Out-Null
    }
}

$cleanupPaths = @(
    'C:\Program Files (x86)\Netskope\STAgent',
    'C:\Program Files\Netskope\STAgent',
    'C:\ProgramData\netskope\stagent',
    'C:\ProgramData\netskope\EPDLP',
    'C:\ProgramData\netskope\DEM',
    (Join-Path $env:LOCALAPPDATA 'Netskope')
)

foreach ($cleanupPath in $cleanupPaths) {
    if (Test-Path -Path $cleanupPath) {
        Remove-Item -Path $cleanupPath -Recurse -Force
    }
}

Write-Host "[cleanup] Cleanup steps are done."
