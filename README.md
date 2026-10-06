# kiosapi-proxy

Multi-KEY rotating OpenAI-compatible proxy for kiosapi.com (bypasses the 15 RPM per-key rate limit).

- Config: `proxy.ini` (server: host/port/base_url, keys: unlimited, model: optional override).
- Run: `python kiosapi.py [--config proxy.ini] [--port 8787]`

## Behavior

- FIFO round-robin key rotation (no interval, no cooldown).
- Rate-limit error (HTTP 429, or body containing limit keywords) -> immediately switch to next key and retry the same request. All keys limited -> return the last 429.
- Any other error (401/403/5xx) -> pass through unchanged.
- Streaming (`"stream":true`) is forwarded chunk-by-chunk; a stream already in flight is not retried.

## Quick start

Double-click `start-proxy.bat` (or stop it with `stop-proxy.bat`), or run manually:

```bat
start "" /min pythonw kiosapi.py --config proxy.ini --port 8787
```

Then confirm it listens:

```bat
netstat -ano | findstr ":8787"
```

## Test examples

Completions (plain):

```powershell
curl.exe -s -m 60 -X POST "http://127.0.0.1:8787/v1/chat/completions" -H "Content-Type: application/json" --% -d "{\"model\":\"deepseek-v4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":16}"
```

Completions (streaming):

```powershell
curl.exe -s -N -m 60 -X POST "http://127.0.0.1:8787/v1/chat/completions" -H "Content-Type: application/json" --% -d "{\"model\":\"deepseek-v4-flash\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":16}"
```

List models:

```powershell
curl.exe -s -m 10 "http://127.0.0.1:8787/v1/models"
```

> Important: In PowerShell 5.1 the JSON body must use `--%` (stop-parsing) with backslash-escaped quotes, and the `-H` header must come BEFORE `--%`. Single-quoted or plain `-d` JSON gets its quotes stripped -> upstream replies `invalid JSON request body`.
>
> Alternative without curling (no quoting pitfalls):

```powershell
$b = @{model="deepseek-v4-flash"; messages=@(@{role="user"; content="hi"}); max_tokens=16} | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "http://127.0.0.1:8787/v1/chat/completions" -Method Post -ContentType "application/json" -Body $b
```

Python one-liner (no curl quoting issues):

```powershell
python -c "import json,urllib.request; req=urllib.request.Request('http://127.0.0.1:8787/v1/chat/completions',data=json.dumps({'model':'deepseek-v4-flash','messages':[{'role':'user','content':'hi'}],'max_tokens':8}).encode(),headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req,timeout=60).read().decode())"
```

## Throughput (verified in controlled simulation)

Per-key quota is independent, so rotation scales linearly:

| scenario | result |
|---|---|
| single key @ effective 30 RPM (cap 15) | 15 ok / 30 limited |
| 5-key rotation @ 75 RPM (aggregate cap 75) | 75/75 ok |

Proxy itself sustains ~3000-4400 RPS at 300-600 client concurrency with zero failures.

## Integration with opencode

Point the provider baseURL at the local proxy and use any placeholder apiKey:

```json
"provider": {
  "kiosapi-local": {
    "npm": "@ai-sdk/openai-compatible",
    "options": {
      "apiKey": "local",
      "baseURL": "http://127.0.0.1:8787/v1"
    }
  }
}
```

(Client path `/v1/...` is deduplicated against base_url so both `http://127.0.0.1:8787` and `http://127.0.0.1:8787/v1` work.)

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

- `test_kiosapi.py`: rotation correctness, 429 handling, streaming, URL join.
- `test_scale.py`: rate-limited fake upstream proves rotation scales throughput.