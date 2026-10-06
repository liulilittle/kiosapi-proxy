@echo off
setlocal
echo [kiosapi-proxy] stopping proxy on port 8787 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R ":8787 .*LISTENING"') do (
  echo [kiosapi-proxy] killing PID %%p
  taskkill /PID %%p /F
)
echo [kiosapi-proxy] done.
endlocal