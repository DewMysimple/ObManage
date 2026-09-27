@echo off
setlocal

rem Build from this checkout, even when launched from another directory.
cd /d "%~dp0" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo [ObManage] Missing .venv\Scripts\python.exe
    echo Install the development dependencies before building.
    exit /b 1
)

rem Stop only this checkout's packaged EXE and wait for it to exit.
powershell.exe -NoProfile -Command "$ErrorActionPreference = 'Stop'; $exe = [IO.Path]::GetFullPath('dist\ObManage\ObManage.exe'); $running = @(Get-CimInstance Win32_Process -Filter 'Name = ''ObManage.exe''' | Where-Object { $_.ExecutablePath -eq $exe }); foreach ($item in $running) { Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[ObManage] Stopped process ' + $item.ProcessId) }; $deadline = (Get-Date).AddSeconds(10); while (Get-CimInstance Win32_Process -Filter 'Name = ''ObManage.exe''' | Where-Object { $_.ExecutablePath -eq $exe }) { if ((Get-Date) -ge $deadline) { throw 'The old EXE did not exit in 10 seconds.' }; Start-Sleep -Milliseconds 200 }"
if errorlevel 1 (
    echo [ObManage] Could not stop or verify dist\ObManage\ObManage.exe. Build cancelled.
    exit /b 1
)

".venv\Scripts\python.exe" "tools\build.py"
if errorlevel 1 (
    echo [ObManage] Build failed.
    exit /b 1
)

echo [ObManage] Built dist\ObManage\ and dist\ObManage.zip
exit /b 0
