@echo off
setlocal
cd /d "%~dp0"

set PORT=8787
set CONFIG=proxy.ini

echo [kiosapi-proxy] checking port %PORT% ...
netstat -ano | findstr /R ":%PORT% .*LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [kiosapi-proxy] already running on port %PORT%, skipping start.
  goto :test
)

echo [kiosapi-proxy] starting (pythonw, no window)...
start "kiosapi-proxy" /min pythonw kiosapi.py --config %CONFIG% --port %PORT%

set n=0
:waitloop
netstat -ano | findstr /R ":%PORT% .*LISTENING" >nul 2>&1
if %errorlevel%==0 goto :ready
set /a n+=1
if %n% geq 20 (
  echo [kiosapi-proxy] FAILED to start within 20s.
  goto :end
)
timeout /t 1 /nobreak >nul
goto :waitloop

:ready
echo [kiosapi-proxy] listening on http://127.0.0.1:%PORT%

:test
echo.
echo === smoke test: GET /v1/models ===
curl -s -m 10 http://127.0.0.1:%PORT%/v1/models
echo.
echo === smoke test: POST /v1/chat/completions (via python, avoids cmd quote issues) ===
python -c "import json,urllib.request; req=urllib.request.Request('http://127.0.0.1:%PORT%/v1/chat/completions',data=json.dumps({'model':'deepseek-v4-flash','messages':[{'role':'user','content':'hi'}],'max_tokens':8}).encode(),headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req,timeout=60).read().decode()[:200])"
echo.
echo [kiosapi-proxy] OK.

:end
endlocal