import argparse
import asyncio
import configparser
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web

LIMIT_KEYWORDS = (
    "too many requests", "rate limit", "rate_limit", "request limit",
    "quota", "freeusagelimiterror", "gousagelimiterror", "free_quota",
    "plan_limit_reached", "insufficient_balance", "insufficient credits",
    "usage limit", "exceeded your current quota",
)

HOP_BY_HOP = {
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "keep-alive", "proxy-connection", "upgrade", "te", "trailer",
    "authorization", "x-api-key", "api-key", "accept-encoding", "user-agent",
    "cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-real-ip", "x-client-ip", "true-client-ip", "cf-connecting-ip",
    "x-cluster-client-ip", "forwarded", "via", "expect", "proxy-authorization",
}

FORWARD_RESPONSE_HEADERS = (
    "content-type", "retry-after", "location", "www-authenticate",
    "x-request-id", "request-id", "trace_id",
    "x-oneapi-request-id", "x-amzn-requestid", "x-goog-request-id",
    "openai-processing-ms",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
)

AUTH_KEYWORDS = (
    "invalid api key", "incorrect api key", "invalid_api_key",
    "api key not valid", "invalid authentication", "authentication fails",
    "invalid_iam_token", "unauthorized", "invalid key", "api key is invalid",
    "please pass a valid api key", "invalid token", "invalid credential",
)

QUOTA_KEYWORDS = (
    "insufficient_quota", "insufficient_balance", "quota_exceeded",
    "quota exceeded", "quotaexceeded", "accountquotaexceeded",
    "setlimitexceeded", "tpdratelimitexceeded", "exceeded_current_quota",
    "plan_limit_reached", "free_quota_daily", "free trial quota",
    "free quota", "usage quota", "quota exhausted",
    "accountoverdue", "account_overdue", "credit_limit_exceeded",
    "usage limit reached", "gousagelimiterror", "freeusagelimiterror",
    "余额不足", "额度已用完", "额度已耗尽", "配额", "欠费",
)

NON_ROTATE_KEYWORDS = (
    "inappropriate_content", "content_moderation", "sensitive_content",
    "policy_violation", "moderation_blocked",
)

AUTH_STYLES = {"bearer", "x-api-key", "api-key", "both"}

SESSION_HEADER_CANDIDATES = (
    "x-opencode-session", "x-session-id", "session_id", "x-session-affinity",
)

SESSION_SALT = os.urandom(16).hex()

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

DEFAULT_COOLDOWN = 60.0
AUTH_COOLDOWN = 3600.0
MAX_COOLDOWN = 3600.0
DEFAULT_MAX_BODY = 64 * 1024 * 1024
POSIX_PERMISSIONS = os.name != "nt"


class ConfigError(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


class KeyPool:
    """Round-robin key pool with per-key cooldown after a rate-limit hit."""

    def __init__(self, keys, default_cooldown=DEFAULT_COOLDOWN):
        self.keys = list(dict.fromkeys(keys))
        self.idx = 0
        self.cooldown_until = {}
        self.default_cooldown = default_cooldown

    def _ordered_from_idx(self):
        n = len(self.keys)
        if n == 0:
            return []
        now = time.monotonic()
        ready, cooling = [], []
        for i in range(n):
            key = self.keys[(self.idx + i) % n]
            if self.cooldown_until.get(key, 0.0) > now:
                cooling.append(key)
            else:
                ready.append(key)
        return ready + cooling

    def candidates(self):
        order = self._ordered_from_idx()
        if self.keys:
            self.idx = (self.idx + 1) % len(self.keys)
        return order

    def ordered_for_persist(self):
        return self._ordered_from_idx()

    def apply_order(self, fingerprints):
        if not fingerprints:
            return
        by_fp = {}
        for key in self.keys:
            by_fp.setdefault(key_fingerprint(key), key)
        ordered = []
        for fingerprint in fingerprints:
            key = by_fp.pop(fingerprint, None)
            if key is not None and key not in ordered:
                ordered.append(key)
        ordered += [key for key in self.keys if key not in ordered]
        self.keys = ordered
        self.idx = 0

    def advance_past(self, key):
        try:
            index = self.keys.index(key)
        except ValueError:
            return
        self.idx = (index + 1) % len(self.keys)

    def mark_limited(self, key, retry_after=None):
        delay = self.default_cooldown
        if retry_after is not None:
            try:
                delay = max(1.0, min(float(retry_after), MAX_COOLDOWN))
            except (TypeError, ValueError):
                pass
        now = time.monotonic()
        newly = self.cooldown_until.get(key, 0.0) <= now
        self.cooldown_until[key] = now + delay
        return newly

    def mark_invalid(self, key):
        return self.mark_limited(key, AUTH_COOLDOWN)

    def replace_keys(self, keys):
        new_keys = list(dict.fromkeys(keys))
        if new_keys == self.keys:
            return
        current = self._ordered_from_idx()
        allowed = set(new_keys)
        ordered = [key for key in current if key in allowed]
        seen = set(ordered)
        ordered += [key for key in new_keys if key not in seen]
        self.keys = ordered
        self.idx = 0
        self.cooldown_until = {
            key: until for key, until in self.cooldown_until.items() if key in allowed}


def key_fingerprint(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mask_key(key, keep=4):
    """Show only the tail of a key; never reveal a key shorter than keep*2."""
    key = key or ""
    if len(key) <= keep * 2:
        return "..."
    return "..." + key[-keep:]


def constant_time_equal(a, b):
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (AttributeError, UnicodeError):
        return False


class Upstream:
    def __init__(self, name, base_url, prefix, keys, model="", user_agent="",
                 timeout=0, auth_style="bearer", session_header="",
                 error_body_path="", error_body_rotate_codes=None,
                 error_body_auth_codes=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix
        self.model = model
        self.user_agent = user_agent
        self.timeout = timeout
        self.auth_style = auth_style
        self.session_header = session_header
        self.error_body_path = error_body_path
        self.error_body_rotate_codes = list(error_body_rotate_codes or [])
        self.error_body_auth_codes = list(error_body_auth_codes or [])
        self.pool = KeyPool(keys)

    @property
    def keys(self):
        return self.pool.keys


def split_list(value):
    return [item for item in re.split(r"[,\s]+", value or "") if item]


def normalize_host(host):
    """Accept [::], [::1], [::]:8787, 127.0.0.1:8787 and plain host forms."""
    host = (host or "").strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")].strip()
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name
    return host


def parse_hosts(value):
    hosts = [normalize_host(item) for item in split_list(value)]
    hosts = [item for item in hosts if item]
    return hosts or ["127.0.0.1"]


def is_loopback(host):
    host = normalize_host(host)
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 6 and addr.ipv4_mapped:
            return addr.ipv4_mapped.is_loopback
        return addr.is_loopback
    except ValueError:
        return False


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError("invalid integer for %s: %s" % (name, value))


def _bool(value, name):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError("invalid boolean for %s: %s" % (name, value))


def is_limit_error(status, body, headers=None):
    """Rate-limit detection: 429/402, or limit keywords in a 4xx error body."""
    if status in (429, 402):
        return True
    if status < 400 or status >= 500 or not body:
        return False
    text = body.decode("utf-8", errors="ignore").lower()
    return any(k in text for k in LIMIT_KEYWORDS)


def is_auth_error(status, body):
    """Auth failure detection, including providers that use 400 for bad keys."""
    if status == 403 and body:
        text = body.decode("utf-8", errors="ignore").lower()
        if any(k in text for k in NON_ROTATE_KEYWORDS):
            return False
    if status in (401, 403):
        return True
    if status not in (400, 422) or not body:
        return False
    text = body.decode("utf-8", errors="ignore").lower()
    return any(k in text for k in AUTH_KEYWORDS)


def cooldown_for(status, body, retry_after):
    """Quota/balance exhaustion should cool much longer than a per-minute limit."""
    if status == 402:
        if retry_after is not None:
            return retry_after
        return AUTH_COOLDOWN
    if body:
        text = body.decode("utf-8", errors="ignore").lower()
        if any(k in text for k in QUOTA_KEYWORDS):
            return AUTH_COOLDOWN
    return retry_after


def parse_retry_after(value):
    """Parse Retry-After as seconds or an HTTP-date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(text)
        if parsed is None:
            return None
        return max(0.0, parsed.timestamp() - time.time())
    except Exception:
        return None


def extract_body_error(body, path):
    """Read a provider business error code from a 200 JSON body."""
    if not path or not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    if isinstance(data, (dict, list)):
        return None
    return data


async def read_body_limited(resp, limit):
    """Read a non-stream body, refusing payloads larger than limit bytes."""
    if limit and limit > 0:
        raw = resp.headers.get("Content-Length")
        if raw is not None:
            try:
                declared = int(raw)
            except (TypeError, ValueError):
                declared = None
            if declared is not None and declared > limit:
                raise ResponseTooLarge(
                    "upstream response Content-Length %d exceeds limit %d"
                    % (declared, limit))
    if not hasattr(resp, "content") or not hasattr(resp.content, "iter_chunked"):
        data = await resp.read()
        if limit and limit > 0 and len(data) > limit:
            raise ResponseTooLarge(
                "upstream response body exceeds limit %d" % limit)
        return data
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        total += len(chunk)
        if limit and limit > 0 and total > limit:
            raise ResponseTooLarge(
                "upstream response body exceeds limit %d" % limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_stream_prefix(resp, limit=65536, timeout=2.0):
    """Read enough of a 200 stream to tell a JSON error body from SSE."""
    buf = b""
    start = time.monotonic()
    try:
        while len(buf) < limit:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(
                resp.content.read(limit - len(buf)), timeout=remaining)
            if not chunk:
                break
            buf += chunk
            stripped = buf.lstrip()
            if not stripped.startswith(b"{"):
                break
            try:
                json.loads(buf)
                break
            except Exception:
                continue
    except Exception:
        pass
    return buf


class PrefixedContent:
    def __init__(self, prefix, content):
        self.prefix = prefix
        self.content = content

    async def iter_chunked(self, size):
        if self.prefix:
            yield self.prefix
        async for chunk in self.content.iter_chunked(size):
            yield chunk


class StreamPayload:
    def __init__(self, resp, prefix=b""):
        self._resp = resp
        self.headers = resp.headers
        self.content = PrefixedContent(prefix, resp.content)

    def release(self):
        self._resp.release()

    def close(self):
        self._resp.close()


def is_stream_request(body):
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("stream"))


def is_certificate_error(ex):
    """Certificate verification failure -> retry with the next key."""
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


def _join_paths(base_path, remainder):
    """Join base path and remainder, dropping the longest duplicated suffix/prefix."""
    if not base_path:
        return remainder or "/"
    if not remainder:
        return base_path
    best = ""
    for i, ch in enumerate(base_path):
        if ch != "/":
            continue
        suffix = base_path[i:]
        if remainder.startswith(suffix) and (
                len(remainder) == len(suffix) or remainder[len(suffix)] == "/"):
            if len(suffix) > len(best):
                best = suffix
    if best:
        return base_path[:len(base_path) - len(best)] + remainder
    if not remainder.startswith("/"):
        remainder = "/" + remainder
    return base_path + remainder


def _merge_query(client_query, base_query):
    """Client query wins; base query params are appended when not overridden."""
    if not client_query:
        return base_query
    if not base_query:
        return client_query
    from urllib.parse import unquote_plus
    seen = set()
    for segment in client_query.split("&"):
        if segment:
            seen.add(unquote_plus(segment.split("=", 1)[0]))
    extra = []
    for segment in base_query.split("&"):
        if not segment:
            continue
        name = unquote_plus(segment.split("=", 1)[0])
        if name in seen:
            continue
        seen.add(name)
        extra.append(segment)
    if not extra:
        return client_query
    return client_query + "&" + "&".join(extra)


def join_upstream_url(base_url, path):
    """Join client path to upstream base_url, avoiding duplicated path prefixes."""
    base = urlsplit(base_url)
    client = urlsplit(path)
    base_path = base.path.rstrip("/")
    upstream_path = _join_paths(base_path, client.path)
    query = _merge_query(client.query, base.query)
    return urlunsplit((base.scheme, base.netloc, upstream_path, query, ""))


def is_placeholder_key(key):
    lower = (key or "").lower()
    return key == "REPLACE_ME" or "xxxx" in lower


def _validate_base_url(value, label):
    if any(ch.isspace() for ch in value):
        raise ConfigError("%s must not contain whitespace: %s" % (label, value))
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("%s must be an http(s) URL: %s" % (label, value))


def resolve_state_path(config_path, value):
    value = (value or "").strip()
    if value.lower() in ("off", "none", "disabled"):
        return ""
    if not value:
        value = os.path.basename(config_path) + ".state.json"
    value = os.path.expanduser(value)
    if os.path.isabs(value):
        return value
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), value)


def load_config(path):
    if os.path.isdir(path):
        raise ConfigError("config path is a directory: " + path)
    cp = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        read = cp.read(path, encoding="utf-8-sig")
    except UnicodeDecodeError as ex:
        raise ConfigError("cannot decode config %s: %s" % (path, ex))
    except configparser.Error as ex:
        raise ConfigError("invalid config: %s" % ex)
    if not read:
        raise ConfigError("config file not found or unreadable: " + path)
    if cp.defaults():
        raise ConfigError("[DEFAULT] options are not supported; put shared values "
                          "in [server] or each upstream section")
    if "server" not in cp:
        raise ConfigError("missing [server] section: " + path)
    if "keys" not in cp:
        raise ConfigError("missing [keys] section: " + path)

    server = cp["server"]
    default_base = server.get("base_url", "https://kiosapi.com/v1").strip().rstrip("/")
    if not default_base:
        raise ConfigError("[server] base_url is empty")
    _validate_base_url(default_base, "[server] base_url")
    keys_sec = cp["keys"]
    keys_line = keys_sec.get("keys", "").strip()
    if keys_line:
        default_keys = split_list(keys_line)
    else:
        default_keys = []
        for k, v in keys_sec.items():
            if k.lower() == "keys" or not k.lower().startswith("key"):
                continue
            default_keys.extend(split_list(v))

    timeout = _int(server.get("timeout", "60"), "server timeout")
    if timeout <= 0:
        raise ConfigError("server timeout must be positive")
    reload_interval = max(1, _int(server.get("reload_interval", "2"), "reload_interval"))
    log_requests = _bool(server.get("log_requests", "false"), "log_requests")
    rotate_auth_errors = _bool(
        server.get("rotate_auth_errors", "true"), "rotate_auth_errors")
    max_body_size = _int(
        server.get("max_body_size", str(DEFAULT_MAX_BODY)), "max_body_size")
    if max_body_size <= 0:
        raise ConfigError("max_body_size must be positive")
    max_response_size = _int(
        server.get("max_response_size", str(DEFAULT_MAX_BODY)),
        "max_response_size")
    if max_response_size < 0:
        raise ConfigError("max_response_size must be >= 0")
    admin_key = server.get("admin_key", "").strip()
    admin_allow_remote = _bool(
        server.get("admin_allow_remote", "false"), "admin_allow_remote")
    auth_keys = list(dict.fromkeys(split_list(server.get("auth_keys", ""))))
    hosts = list(dict.fromkeys(parse_hosts(server.get("host", "127.0.0.1"))))
    port = _int(server.get("port", "8787"), "server port")
    if not 1 <= port <= 65535:
        raise ConfigError("server port out of range: %d" % port)
    state_file = resolve_state_path(path, server.get("state_file", ""))
    if state_file and os.path.normcase(os.path.realpath(state_file)) == \
            os.path.normcase(os.path.realpath(path)):
        raise ConfigError("state_file must not be the config file")
    if state_file and os.path.isdir(state_file):
        raise ConfigError("state_file is a directory: " + state_file)
    if admin_key and admin_key in auth_keys:
        raise ConfigError("admin_key must differ from auth_keys")
    model = cp.get("model", "name", fallback="").strip() if cp.has_section("model") else ""

    default = Upstream("default", default_base, "/", default_keys,
                       model=model, timeout=timeout)

    upstreams = []
    seen_names = set()
    seen_prefixes = {}
    for section in cp.sections():
        if not section.startswith("upstream:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            raise ConfigError("empty upstream name in [" + section + "]")
        if name in ("default", "keys"):
            raise ConfigError("upstream name '%s' is reserved" % name)
        if not re.match(r"^[A-Za-z0-9._-]+$", name):
            raise ConfigError("invalid upstream name: " + name)
        if name in seen_names:
            raise ConfigError("duplicate upstream name: " + name)
        seen_names.add(name)
        sec = cp[section]
        base_url = sec.get("base_url", "").strip().rstrip("/")
        if not base_url:
            raise ConfigError("[" + section + "] missing base_url")
        _validate_base_url(base_url, "[" + section + "] base_url")
        prefix = sec.get("prefix", "/" + name).strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        prefix = prefix.rstrip("/") or "/"
        if prefix == "/":
            raise ConfigError("[" + section + "] prefix '/' is reserved for the default upstream")
        if "?" in prefix or "#" in prefix or any(ch.isspace() for ch in prefix):
            raise ConfigError("[" + section + "] invalid prefix: " + prefix)
        if prefix in seen_prefixes:
            raise ConfigError("duplicate prefix %s in [%s] and [%s]"
                              % (prefix, seen_prefixes[prefix], section))
        seen_prefixes[prefix] = section
        keys = split_list(sec.get("keys", ""))
        if not keys:
            for k, v in sec.items():
                if k.lower() == "keys" or not k.lower().startswith("key"):
                    continue
                keys.extend(split_list(v))
        keys = list(dict.fromkeys(keys))
        auth_style = sec.get("auth_style", "bearer").strip().lower()
        if auth_style not in AUTH_STYLES:
            raise ConfigError("[" + section + "] invalid auth_style: " + auth_style)
        session_header = sec.get("session_header", "").strip()
        if session_header and not re.match(r"^[A-Za-z0-9-]+$", session_header):
            raise ConfigError("[" + section + "] invalid session_header: " + session_header)
        up_timeout = _int(sec.get("timeout", str(timeout)), section + " timeout")
        if up_timeout < 0:
            raise ConfigError("[" + section + "] timeout must be >= 0")
        upstreams.append(Upstream(
            name=name, base_url=base_url, prefix=prefix, keys=keys,
            model=sec.get("model", "").strip(),
            user_agent=sec.get("user_agent", "").strip(),
            timeout=up_timeout,
            auth_style=auth_style,
            session_header=session_header,
            error_body_path=sec.get("error_body_path", "").strip(),
            error_body_rotate_codes=split_list(sec.get("error_body_rotate_codes", "")),
            error_body_auth_codes=split_list(sec.get("error_body_auth_codes", ""))))

    return SimpleNamespace(
        path=path, hosts=hosts, port=port, auth_keys=auth_keys,
        reload_interval=reload_interval, timeout=timeout,
        log_requests=log_requests, state_file=state_file,
        rotate_auth_errors=rotate_auth_errors,
        max_body_size=max_body_size,
        max_response_size=max_response_size,
        admin_key=admin_key, admin_allow_remote=admin_allow_remote,
        default=default, upstreams=upstreams)


def client_key(headers):
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key", "").strip()


def _header_value(headers, name):
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def build_upstream_headers(client_headers, upstream, key, content_type):
    headers = {}
    for name, value in client_headers.items():
        if name.lower() in HOP_BY_HOP:
            continue
        headers[name] = value
    style = upstream.auth_style
    if style in ("bearer", "both"):
        headers["Authorization"] = "Bearer " + key
    if style in ("x-api-key", "both"):
        headers["x-api-key"] = key
    if style == "api-key":
        headers["api-key"] = key
    if upstream.session_header and not _header_value(headers, upstream.session_header):
        session = ""
        for candidate in SESSION_HEADER_CANDIDATES:
            session = _header_value(headers, candidate)
            if session:
                break
        if not session:
            material = client_key(client_headers) + SESSION_SALT
            session = "ses_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        headers[upstream.session_header] = session
    headers["Content-Type"] = content_type or "application/json"
    headers["User-Agent"] = upstream.user_agent or DEFAULT_UA
    return headers


def copy_response_headers(target, source):
    if not source:
        return
    for name in FORWARD_RESPONSE_HEADERS:
        value = source.get(name)
        if value:
            target[name] = value


def file_signature(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class ProxyEngine:
    """Forward requests to the routed upstream with key rotation on rate limits."""

    def __init__(self, config):
        self.config = config
        self.session = None
        self._tasks = []
        default = getattr(config, "default", None)
        if default is None:
            default = Upstream(
                "default", config.base_url, "/", config.keys,
                model=getattr(config, "model", ""),
                timeout=getattr(config, "timeout", 60))
        self.default = default
        self.upstreams = {u.name: u for u in getattr(config, "upstreams", [])}
        self.auth_keys = list(getattr(config, "auth_keys", []))
        self.admin_key = getattr(config, "admin_key", "")
        self.admin_allow_remote = getattr(config, "admin_allow_remote", False)
        self.rotate_auth_errors = getattr(config, "rotate_auth_errors", True)
        self.state_path = getattr(config, "state_file", "") or ""
        self.max_response_size = getattr(
            config, "max_response_size", DEFAULT_MAX_BODY)
        self._state_dirty = False
        self._last_state_save = 0.0
        self._state_warned = False
        self._load_state()

    def _pools(self):
        yield "default", self.default.pool
        for name, upstream in self.upstreams.items():
            yield name, upstream.pool

    def _load_state(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        saved = data.get("upstreams")
        if not isinstance(saved, dict):
            return
        for name, pool in self._pools():
            fingerprints = saved.get(name)
            if isinstance(fingerprints, list):
                pool.apply_order([fp for fp in fingerprints if isinstance(fp, str)])

    def save_state(self):
        if not self.state_path:
            self._state_dirty = False
            return
        data = {"version": 1, "upstreams": {}}
        for name, pool in self._pools():
            data["upstreams"][name] = [
                key_fingerprint(key) for key in pool.ordered_for_persist()]
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            if POSIX_PERMISSIONS:
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            os.replace(tmp, self.state_path)
            self._state_dirty = False
            self._last_state_save = time.monotonic()
        except OSError as ex:
            self._last_state_save = time.monotonic()
            if not self._state_warned:
                print("[kiosapi-proxy] state save failed: %s" % ex, flush=True)
                self._state_warned = True

    async def watch_state(self):
        while True:
            await asyncio.sleep(0.5)
            if self._state_dirty and time.monotonic() - self._last_state_save >= 1.0:
                self.save_state()

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            connector=aiohttp.TCPConnector(limit=0),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self._tasks.append(asyncio.create_task(self.watch_config()))
        self._tasks.append(asyncio.create_task(self.watch_state()))

    async def close(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                print("[kiosapi-proxy] background task error: %s" % ex, flush=True)
        self.save_state()
        if self.session:
            await self.session.close()

    def authorized(self, key):
        if not self.auth_keys:
            return True
        return any(constant_time_equal(key, expected) for expected in self.auth_keys)

    def route(self, path):
        path_only, sep, query = path.partition("?")
        suffix = sep + query
        best = None
        for upstream in self.upstreams.values():
            prefix = upstream.prefix
            if prefix == "/":
                continue
            if path_only == prefix or path_only.startswith(prefix + "/"):
                if best is None or len(prefix) > len(best.prefix):
                    best = upstream
        if best is None:
            if path_only == "/kiosapi" or path_only.startswith("/kiosapi/"):
                remainder = path_only[len("/kiosapi"):] or "/"
                return self.default, remainder + suffix
            return self.default, path
        remainder = path_only[len(best.prefix):] or "/"
        if not remainder.startswith("/"):
            remainder = "/" + remainder
        return best, remainder + suffix

    def _apply_model(self, body, upstream):
        model = upstream.model
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

    def _log_attempt(self, upstream, key, status):
        if getattr(self.config, "log_requests", False):
            print("[kiosapi-proxy] request upstream=%s key=%s status=%s"
                  % (upstream.name, mask_key(key), status), flush=True)

    async def forward(self, method, path, body, content_type, headers=None):
        headers = headers or {}
        upstream, upstream_path = self.route(path)
        url = join_upstream_url(upstream.base_url, upstream_path)
        body = self._apply_model(body, upstream)
        stream = is_stream_request(body)

        last_status, last_body = 502, json.dumps(
            {"error": {"message": "upstream unavailable", "type": "proxy_error"}}).encode()
        last_headers = None
        candidates = upstream.pool.candidates()
        first = candidates[0] if candidates else None
        if not candidates:
            return ("plain", 503, json.dumps({
                "error": {
                    "message": "no keys configured for upstream '%s'" % upstream.name,
                    "type": "proxy_config_error",
                }}).encode(), None)
        self._state_dirty = True

        for key in candidates:
            req_headers = build_upstream_headers(headers, upstream, key, content_type)
            resp = None
            released = False
            try:
                limit = upstream.timeout or self.config.timeout
                if stream:
                    timeout = aiohttp.ClientTimeout(
                        total=None, sock_connect=limit, sock_read=limit)
                else:
                    timeout = aiohttp.ClientTimeout(total=limit)
                resp = await self.session.request(
                    method, url, data=body, headers=req_headers, timeout=timeout,
                    allow_redirects=False)
                self._log_attempt(upstream, key, resp.status)
                if stream and resp.status == 200:
                    prefix = b""
                    if upstream.error_body_path:
                        prefix = await read_stream_prefix(resp)
                        code = extract_body_error(prefix, upstream.error_body_path)
                        if code is not None:
                            code_text = str(code)
                            if code_text in upstream.error_body_auth_codes:
                                if upstream.pool.mark_invalid(key):
                                    print("[kiosapi-proxy] upstream=%s key=%s stream body auth error (%s), cooling"
                                          % (upstream.name, mask_key(key), code_text), flush=True)
                                upstream.pool.advance_past(key)
                                last_status, last_body = 401, prefix
                                last_headers = resp.headers.copy()
                                resp.close()
                                continue
                            if code_text in upstream.error_body_rotate_codes:
                                if upstream.pool.mark_limited(key, AUTH_COOLDOWN):
                                    print("[kiosapi-proxy] upstream=%s key=%s stream body limit (%s), cooling"
                                          % (upstream.name, mask_key(key), code_text), flush=True)
                                upstream.pool.advance_past(key)
                                last_status, last_body = 429, prefix
                                last_headers = resp.headers.copy()
                                resp.close()
                                continue
                    if key != first:
                        upstream.pool.advance_past(key)
                    return ("stream", resp.status, StreamPayload(resp, prefix), resp)
                try:
                    data = await read_body_limited(resp, self.max_response_size)
                except ResponseTooLarge as ex:
                    if not released:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    print("[kiosapi-proxy] upstream=%s rejected oversized response: %s"
                          % (upstream.name, ex), flush=True)
                    return ("plain", 502, json.dumps(
                        {"error": {"message": "upstream response too large",
                                   "type": "proxy_error"}}).encode(), None)
                resp_headers = resp.headers.copy()
                retry_after = parse_retry_after(resp_headers.get("Retry-After"))
                resp.release()
                released = True

                if resp.status == 200 and not stream and upstream.error_body_path:
                    code = extract_body_error(data, upstream.error_body_path)
                    if code is not None:
                        code_text = str(code)
                        if code_text in upstream.error_body_auth_codes:
                            if upstream.pool.mark_invalid(key):
                                print("[kiosapi-proxy] upstream=%s key=%s body auth error (%s), cooling"
                                      % (upstream.name, mask_key(key), code_text), flush=True)
                            upstream.pool.advance_past(key)
                            last_status, last_body = 401, data
                            last_headers = resp_headers
                            continue
                        if code_text in upstream.error_body_rotate_codes:
                            if upstream.pool.mark_limited(key, AUTH_COOLDOWN):
                                print("[kiosapi-proxy] upstream=%s key=%s body limit (%s), cooling"
                                      % (upstream.name, mask_key(key), code_text), flush=True)
                            upstream.pool.advance_past(key)
                            last_status, last_body = 429, data
                            last_headers = resp_headers
                            continue

                if not is_limit_error(resp.status, data):
                    if is_auth_error(resp.status, data) and self.rotate_auth_errors:
                        if upstream.pool.mark_invalid(key):
                            print("[kiosapi-proxy] upstream=%s key=%s auth error (%s), cooling"
                                  % (upstream.name, mask_key(key), resp.status), flush=True)
                        upstream.pool.advance_past(key)
                        last_status, last_body = resp.status, data
                        last_headers = resp_headers
                        continue
                    if key != first:
                        upstream.pool.advance_past(key)
                    return ("plain", resp.status, data, resp_headers)

                cooldown = cooldown_for(resp.status, data, retry_after)
                if upstream.pool.mark_limited(key, cooldown):
                    print("[kiosapi-proxy] upstream=%s key=%s limited (%s), cooling"
                          % (upstream.name, mask_key(key), resp.status), flush=True)
                upstream.pool.advance_past(key)
                last_status, last_body = resp.status, data
                last_headers = resp_headers
            except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError,
                    asyncio.TimeoutError, ssl.SSLCertVerificationError) as ex:
                if resp is not None and not released:
                    try:
                        resp.close()
                    except Exception:
                        pass
                self._log_attempt(upstream, key, "ERR")
                if is_certificate_error(ex):
                    upstream.pool.advance_past(key)
                    last_status, last_body = 502, json.dumps(
                        {"error": {"message": "upstream certificate error",
                                   "type": "certificate_error"}}).encode()
                    continue
                return ("plain", 502, json.dumps(
                    {"error": {"message": "upstream connection error",
                               "type": "proxy_error"}}).encode(), None)
            except asyncio.CancelledError:
                if resp is not None and not released:
                    try:
                        resp.close()
                    except Exception:
                        pass
                raise
            except Exception:
                if resp is not None and not released:
                    try:
                        resp.close()
                    except Exception:
                        pass
                raise

        return ("plain", last_status, last_body, last_headers)

    def apply_config(self, new_config):
        old = self.config
        if list(new_config.hosts) != list(old.hosts) or new_config.port != old.port:
            print("[kiosapi-proxy] host/port changes require restart; keeping current bind",
                  flush=True)
        if new_config.max_body_size != old.max_body_size:
            print("[kiosapi-proxy] max_body_size changes require restart; "
                  "keeping current limit", flush=True)
        bound_hosts = list(old.hosts)
        if not new_config.auth_keys and any(not is_loopback(h) for h in bound_hosts):
            raise ConfigError(
                "auth_keys is empty while bound to non-loopback address; reload rejected")
        if any(not is_loopback(h) for h in bound_hosts) and any(
                is_placeholder_key(key) for key in new_config.auth_keys):
            raise ConfigError(
                "auth_keys contains a placeholder while bound to non-loopback "
                "address; reload rejected")
        if new_config.admin_allow_remote and new_config.admin_key and \
                is_placeholder_key(new_config.admin_key):
            raise ConfigError(
                "admin_allow_remote requires a non-placeholder admin_key; reload rejected")
        new_config.hosts = bound_hosts
        new_config.port = old.port
        self.config = new_config
        self.auth_keys = list(new_config.auth_keys)
        self.admin_key = getattr(new_config, "admin_key", "")
        self.admin_allow_remote = getattr(new_config, "admin_allow_remote", False)
        self.rotate_auth_errors = getattr(new_config, "rotate_auth_errors", True)
        self.max_response_size = getattr(
            new_config, "max_response_size", DEFAULT_MAX_BODY)
        new_state_path = getattr(new_config, "state_file", "") or ""
        if new_state_path != self.state_path:
            self._state_warned = False
        self.state_path = new_state_path
        self.default.base_url = new_config.default.base_url
        self.default.model = new_config.default.model
        self.default.timeout = new_config.default.timeout
        self.default.pool.replace_keys(new_config.default.keys)

        new_names = set()
        for upstream in new_config.upstreams:
            new_names.add(upstream.name)
            existing = self.upstreams.get(upstream.name)
            if existing is None:
                self.upstreams[upstream.name] = upstream
            else:
                existing.base_url = upstream.base_url
                existing.prefix = upstream.prefix
                existing.model = upstream.model
                existing.user_agent = upstream.user_agent
                existing.timeout = upstream.timeout
                existing.auth_style = upstream.auth_style
                existing.session_header = upstream.session_header
                existing.error_body_path = upstream.error_body_path
                existing.error_body_rotate_codes = list(upstream.error_body_rotate_codes)
                existing.error_body_auth_codes = list(upstream.error_body_auth_codes)
                existing.pool.replace_keys(upstream.keys)
        for name in list(self.upstreams):
            if name not in new_names:
                del self.upstreams[name]
        self._state_dirty = True
        print("[kiosapi-proxy] config reloaded: %d upstreams, %d auth keys"
              % (len(self.upstreams), len(self.auth_keys)), flush=True)

    async def watch_config(self):
        last = file_signature(self.config.path)
        while True:
            interval = self.config.reload_interval
            if interval <= 0:
                return
            await asyncio.sleep(interval)
            signature = file_signature(self.config.path)
            if signature == last:
                continue
            last = signature
            try:
                self.apply_config(load_config(self.config.path))
            except ConfigError as ex:
                print("[kiosapi-proxy] config reload failed: %s; keeping previous config"
                      % ex, flush=True)
            except Exception as ex:
                print("[kiosapi-proxy] config reload error: %s; keeping previous config"
                      % ex, flush=True)


async def admin_reload(request):
    engine = request.app["engine"]
    if not engine.admin_key:
        return web.json_response({"error": "admin disabled"}, status=404)
    if not constant_time_equal(client_key(request.headers), engine.admin_key):
        return web.json_response({"error": "unauthorized"}, status=401)
    remote = request.remote or ""
    if not engine.admin_allow_remote and not is_loopback(remote):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        engine.apply_config(load_config(engine.config.path))
    except ConfigError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response({"ok": True, "upstreams": len(engine.upstreams)})


async def handle(request):
    engine = request.app["engine"]
    if not engine.authorized(client_key(request.headers)):
        return web.json_response(
            {"error": {"message": "invalid proxy key", "type": "authentication_error"}},
            status=401)
    body = await request.read()
    content_type = request.headers.get("Content-Type", "application/json")
    try:
        kind, status, payload, headers = await engine.forward(
            request.method, request.raw_path, body, content_type, request.headers)
    except Exception as ex:
        print("[kiosapi-proxy] internal error: %s" % type(ex).__name__, flush=True)
        kind, status, payload = "plain", 500, json.dumps(
            {"error": {"message": "proxy internal error",
                       "type": "proxy_internal_error"}}).encode()
        headers = None

    if kind == "stream":
        resp = web.StreamResponse(status=status)
        try:
            copy_response_headers(resp.headers, payload.headers)
            if "Content-Type" not in resp.headers:
                resp.headers["Content-Type"] = "text/event-stream"
            await resp.prepare(request)
        except asyncio.CancelledError:
            payload.close()
            raise
        except Exception:
            payload.close()
            raise
        completed = False
        try:
            async for chunk in payload.content.iter_chunked(8192):
                await resp.write(chunk)
            completed = True
        except (aiohttp.ClientError, ConnectionError, OSError,
                asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            try:
                if completed:
                    payload.release()
                else:
                    payload.close()
            except Exception:
                pass
        try:
            await resp.write_eof()
        except (ConnectionError, OSError):
            pass
        return resp

    hdrs = {}
    if headers:
        copy_response_headers(hdrs, headers)
    if not any(name.lower() == "content-type" for name in hdrs):
        hdrs["Content-Type"] = "application/json"
    return web.Response(status=status, body=payload, headers=hdrs)


def make_listen_socket(host, port):
    host = normalize_host(host)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.create_server(
        (host, port), family=family, backlog=128, dualstack_ipv6=False)
    sock.setblocking(False)
    return sock


async def start_sites(runner, hosts, port):
    sites = []
    for host in hosts:
        sock = None
        try:
            sock = make_listen_socket(host, port)
            site = web.SockSite(runner, sock)
            await site.start()
            sites.append((host, site))
            shown = "[%s]" % host if ":" in host else host
            print("[kiosapi-proxy] listening on http://%s:%d" % (shown, port), flush=True)
        except (OSError, ValueError) as ex:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            print("[kiosapi-proxy] bind %s:%d failed: %s" % (host, port, ex), flush=True)
    if not sites:
        raise ConfigError("no listen address could be bound")
    return sites


async def main():
    parser = argparse.ArgumentParser(
        description="kiosapi-proxy: multi-upstream rotating OpenAI-compatible proxy")
    parser.add_argument("--config", default="proxy.ini", help="ini config file path")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as ex:
        sys.exit("[ERROR] " + str(ex))
    port = args.port or config.port
    if args.port is not None and not 1 <= args.port <= 65535:
        sys.exit("[ERROR] --port out of range")

    if not config.auth_keys and any(not is_loopback(h) for h in config.hosts):
        sys.exit("[ERROR] auth_keys is empty but host binds a non-loopback address; "
                 "refusing to start an open proxy")
    if any(is_placeholder_key(key) for key in config.auth_keys) and any(
            not is_loopback(h) for h in config.hosts):
        sys.exit("[ERROR] auth_keys contains a placeholder and host is non-loopback; "
                 "refusing to expose a known key")
    if config.admin_allow_remote and config.admin_key and is_placeholder_key(config.admin_key):
        sys.exit("[ERROR] admin_allow_remote requires a non-placeholder admin_key")

    placeholder_keys = sum(
        1 for upstream in [config.default] + list(config.upstreams)
        for key in upstream.keys if is_placeholder_key(key))
    if placeholder_keys:
        print("[kiosapi-proxy] WARNING: %d placeholder key(s) configured; "
              "replace them before switching" % placeholder_keys, flush=True)
    if any(is_placeholder_key(key) for key in config.auth_keys):
        print("[kiosapi-proxy] WARNING: auth_keys contains a placeholder", flush=True)
    if config.admin_key and is_placeholder_key(config.admin_key):
        print("[kiosapi-proxy] WARNING: admin_key looks like a placeholder", flush=True)

    engine = ProxyEngine(config)
    await engine.start()

    app = web.Application(client_max_size=config.max_body_size)
    app["engine"] = engine
    app.router.add_post("/_admin/reload", admin_reload)
    app.router.add_route("*", "/{tail:.*}", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await start_sites(runner, config.hosts, port)
    except ConfigError as ex:
        await engine.close()
        await runner.cleanup()
        sys.exit("[ERROR] " + str(ex))

    names = [config.default.name] + [u.name for u in config.upstreams]
    print("[kiosapi-proxy] upstreams: %s" % ", ".join(names), flush=True)
    for upstream in [config.default] + list(config.upstreams):
        print("[kiosapi-proxy]   %s: %d keys" % (upstream.name, len(upstream.keys)),
              flush=True)
    print("[kiosapi-proxy] auth_keys=%d reload_interval=%ds admin=%s state_file=%s"
          % (len(config.auth_keys), config.reload_interval,
             "on" if config.admin_key else "off", config.state_file or "off"), flush=True)
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
