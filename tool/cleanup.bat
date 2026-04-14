@echo off
setlocal

echo [cleanup] Starting Netskope environment cleanup...

taskkill /f /im stAgentUI.exe >nul 2>&1
taskkill /f /im stAgentSvcMon.exe >nul 2>&1
taskkill /f /im stAgentSvc.exe >nul 2>&1
taskkill /f /im msiexec.exe >nul 2>&1

sc stop stwatchdog >nul 2>&1
sc delete stwatchdog >nul 2>&1

sc stop stAgentSvc >nul 2>&1
sc delete stAgentSvc >nul 2>&1

sc stop stadrv >nul 2>&1
sc delete stadrv >nul 2>&1

timeout /t 3 /nobreak >nul

if exist "C:\Program Files (x86)\Netskope\STAgent\netskopecleanup.exe" (
	"C:\Program Files (x86)\Netskope\STAgent\netskopecleanup.exe" >nul 2>&1
)
if exist "C:\Program Files\Netskope\STAgent\netskopecleanup.exe" (
	"C:\Program Files\Netskope\STAgent\netskopecleanup.exe" >nul 2>&1
)

set "appPath=C:\Program Files (x86)\Netskope\STAgent"
if exist "%appPath%" (
	del /s /q "%appPath%\*.*" >nul 2>&1
	for /d %%D in ("%appPath%\*") do rd /s /q "%%D" >nul 2>&1
)

set "appPath64=C:\Program Files\Netskope\STAgent"
if exist "%appPath64%" (
	del /s /q "%appPath64%\*.*" >nul 2>&1
	for /d %%D in ("%appPath64%\*") do rd /s /q "%%D" >nul 2>&1
)

echo [cleanup] Cleanup steps are done.
endlocal
