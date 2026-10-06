import argparse
import asyncio
import configparser
import json
import ssl
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web

LIMIT_KEYWORDS = ("too many requests", "rate limit", "rate_limit", "request limit")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class KeyPool:
    """FIFO round-robin KEY pool: no interval, no cooldown, only index advance."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.idx = 0

    def next(self):
        key = self.keys[self.idx % len(self.keys)]
        self.idx += 1
        return key


def is_limit_error(status, body):
    """Rate-limit detection: HTTP 429 or body contains limit keywords."""
    if status == 429:
        return True
    if body:
        text = body.decode("utf-8", errors="ignore").lower()
        return any(k in text for k in LIMIT_KEYWORDS)
    return False


def is_stream_request(body):
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("stream"))


def is_certificate_error(ex):
    """Certificate verification failure -> retry with the next key.

    Covers ssl.SSLCertVerificationError and aiohttp's ClientConnectorCertificateError,
    whose text usually contains 'certificate' / 'unknown certificate verification error'.
    """
    if isinstance(ex, ssl.SSLCertVerificationError):
        return True
    try:
        from aiohttp import ClientConnectorCertificateError
        if isinstance(ex, ClientConnectorCertificateError):
            return True
    except Exception:
        pass
    name = type(ex).__name__.lower()
    text = str(ex).lower()
    return "certificate" in name or "certificate" in text


def join_upstream_url(base_url, path):
    """Join client path to upstream base_url, avoiding duplicated path prefixes.

    Example: base_url=https://kiosapi.com/v1 and client path /v1/chat/completions
    should become https://kiosapi.com/v1/chat/completions, not /v1/v1/chat/completions.
    """
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    if path.startswith(base_path + "/"):
        remainder = path[len(base_path):]
    elif path == base_path:
        remainder = ""
    else:
        remainder = path
    if not remainder.startswith("/") and remainder:
        remainder = "/" + remainder
    upstream_path = (base_path + remainder) or "/"
    return urlunsplit((base.scheme, base.netloc, upstream_path, base.query, base.fragment))


def load_config(path):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if "server" not in cp:
        sys.exit("[ERROR] config missing [server] section: " + path)
    if "keys" not in cp:
        sys.exit("[ERROR] config missing [keys] section: " + path)
    server = cp["server"]
    keys = [v.strip() for v in cp["keys"].values() if v.strip()]
    if not keys:
        sys.exit("[ERROR] [keys] section is empty; at least 1 KEY required")
    model = cp.get("model", "name", fallback="").strip() if cp.has_section("model") else ""
    return SimpleNamespace(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", "8787")),
        base_url=server.get("base_url", "https://kiosapi.com/v1").rstrip("/"),
        keys=keys,
        model=model,
        timeout=int(server.get("timeout", "60")),
    )


class ProxyEngine:
    """Forward requests with FIFO key rotation; switch key on rate-limit error, pass through other errors."""

    def __init__(self, config):
        self.config = config
        self.pool = KeyPool(config.keys)
        self.session = None

    async def start(self):
        timeout = __import__("aiohttp").ClientTimeout(total=self.config.timeout)
        self.session = __import__("aiohttp").ClientSession(
            timeout=timeout,
            connector=__import__("aiohttp").TCPConnector(limit=0, enable_cleanup_closed=True),
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def forward(self, method, path, body, content_type):
        url = join_upstream_url(self.config.base_url, path)
        body = self._apply_model(body)
        stream = is_stream_request(body)

        last_status, last_body = 502, json.dumps(
            {"error": {"message": "upstream unavailable", "type": "proxy_error"}}).encode()

        for _ in range(len(self.pool.keys)):
            key = self.pool.next()
            headers = {
                "Authorization": "Bearer " + key,
                "Content-Type": content_type or "application/json",
                "User-Agent": DEFAULT_UA,
            }
            try:
                resp = await self.session.request(method, url, data=body, headers=headers)
                if stream and resp.status == 200:
                    return ("stream", resp.status, resp, resp)
                data = await resp.read()
                resp.release()
                if not is_limit_error(resp.status, data):
                    return ("plain", resp.status, data, resp.headers)
                last_status, last_body = resp.status, data
            except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError, ssl.SSLCertVerificationError) as ex:
                if is_certificate_error(ex):
                    last_status, last_body = 502, json.dumps(
                        {"error": {"message": str(ex), "type": "certificate_error"}}).encode()
                    continue
                return ("plain", 502, json.dumps(
                    {"error": {"message": str(ex), "type": "proxy_error"}}).encode(), None)

        return ("plain", last_status, last_body, None)

    def _apply_model(self, body):
        model = self.config.model
        if not model or not body:
            return body
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return body
        if isinstance(data, dict):
            data["model"] = model
            return json.dumps(data).encode("utf-8")
        return body


async def handle(request):
    engine = request.app["engine"]
    body = await request.read()
    content_type = request.headers.get("Content-Type", "application/json")
    try:
        kind, status, payload, headers = await engine.forward(
            request.method, request.path, body, content_type)
    except Exception as ex:
        kind, status, payload = "plain", 500, json.dumps(
            {"error": {"message": str(ex), "type": "proxy_internal_error"}}).encode()
        headers = None

    if kind == "stream":
        resp = web.StreamResponse(status=status)
        resp.headers["Content-Type"] = payload.headers.get("Content-Type", "text/event-stream")
        await resp.prepare(request)
        try:
            async for chunk in payload.content.iter_chunked(8192):
                await resp.write(chunk)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            try:
                await payload.release()
            except Exception:
                pass
        try:
            await resp.write_eof()
        except (ConnectionError, OSError):
            pass
        return resp

    hdrs = {"Content-Type": "application/json"}
    if headers and headers.get("Content-Type"):
        hdrs["Content-Type"] = headers["Content-Type"]
    return web.Response(status=status, body=payload, headers=hdrs)


async def main():
    parser = argparse.ArgumentParser(description="kiosapi-proxy: multi-KEY rotating OpenAI-compatible proxy")
    parser.add_argument("--config", default="proxy.ini", help="ini config file path")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or config.port

    engine = ProxyEngine(config)
    await engine.start()

    app = web.Application()
    app["engine"] = engine
    app.router.add_route("*", "/{tail:.*}", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, port)
    await site.start()
    print(f"[kiosapi-proxy] listening on http://{config.host}:{port}", flush=True)
    print(f"[kiosapi-proxy] upstream {config.base_url}, keys={len(config.keys)}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await engine.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
