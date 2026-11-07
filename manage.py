"""Command-line key management for kiosapi-proxy.

Edits proxy.ini in place (preserving comments), then asks the running proxy to
reload immediately through POST /_admin/reload. Falls back to the file watcher
when the admin endpoint is unavailable.

Examples:
  python manage.py list
  python manage.py show kenari
  python manage.py add-key kenari kn-xxxx
  python manage.py add-keys kenari kn-a kn-b
  python manage.py set-key kenari 2 kn-new
  python manage.py remove-key kenari 2
  python manage.py remove-keys kenari abc --yes
  python manage.py clear-keys kenari --yes
  python manage.py clear-all-keys --yes
  python manage.py state
  python manage.py check
  python manage.py validate
  python manage.py reload
"""

import argparse
import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.request

import kiosapi


def mask_key(key, reveal=False):
    if reveal:
        return key
    if len(key) <= 4:
        return "..."
    if len(key) <= 12:
        return "..." + key[-4:]
    return key[:6] + "..." + key[-4:]


def valid_key(key):
    return bool(key) and not any(ch.isspace() for ch in key) and "," not in key


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _open_no_redirect(req, timeout):
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


def normalize_upstream(name):
    name = name.strip()
    if name in ("default", "keys", "[keys]"):
        return "default"
    if name.startswith("upstream:"):
        name = name[len("upstream:"):].strip()
    return name


def section_name(upstream):
    return "keys" if upstream == "default" else "upstream:" + upstream


def read_lines(path):
    if os.path.isdir(path):
        raise ValueError("config path is a directory: " + path)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        raise ValueError("config not found: " + path)
    except UnicodeDecodeError as ex:
        raise ValueError("cannot decode config %s: %s" % (path, ex))


def write_lines(path, lines):
    newline = "\n"
    try:
        with open(path, "rb") as fh:
            if b"\r\n" in fh.read():
                newline = "\r\n"
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(newline.join(lines) + newline)
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def find_section(lines, name):
    header_re = re.compile(r"^\s*\[([^\]]+)\]")
    start = None
    for i, line in enumerate(lines):
        match = header_re.match(line)
        if match and match.group(1).strip() == name:
            start = i
            break
    if start is None:
        raise KeyError("section not found: [" + name + "]")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if header_re.match(lines[j]):
            end = j
            break
    return start, end


def strip_inline_comment(value):
    for i, ch in enumerate(value):
        if ch in (";", "#") and (i == 0 or value[i - 1].isspace()):
            return value[:i].strip()
    return value.strip()


def section_keys(lines, name):
    cp = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cp.read_string("\n".join(lines) + "\n")
    except configparser.Error as ex:
        raise ValueError("invalid config: %s" % ex)
    if not cp.has_section(name):
        raise KeyError("section not found: [" + name + "]")
    if cp.defaults():
        raise ValueError("[DEFAULT] options are not supported")
    sec = cp[name]
    keys_line = sec.get("keys", "").strip()
    if keys_line:
        keys = kiosapi.split_list(keys_line)
    else:
        keys = []
        for option, value in sec.items():
            if option.lower() == "keys" or not option.lower().startswith("key"):
                continue
            keys.extend(kiosapi.split_list(value))
    return list(dict.fromkeys(keys))


def set_section_keys(lines, name, keys):
    start, end = find_section(lines, name)
    body = []
    skip_continuation = False
    for i in range(start + 1, end):
        line = lines[i]
        if skip_continuation:
            stripped = line.strip()
            if not stripped or stripped.startswith((";", "#")):
                body.append(line)
                continue
            if line[:1].isspace():
                continue
            skip_continuation = False
        if re.match(r"^\s*(keys|key\S*)\s*[=:]", line, re.IGNORECASE):
            skip_continuation = True
            continue
        body.append(line)
    if name == "keys":
        new_body = body + ["key%d = %s" % (i + 1, key) for i, key in enumerate(keys)]
    else:
        pos = 0
        for i, line in enumerate(body):
            if re.match(r"^\s*base_url\s*=", line):
                pos = i + 1
                break
        new_body = body[:pos] + ["keys = " + ", ".join(keys)] + body[pos:]
    lines[start + 1:end] = new_body


def all_upstreams(lines):
    names = []
    if any(re.match(r"^\s*\[keys\]", line) for line in lines):
        names.append("default")
    header_re = re.compile(r"^\s*\[upstream:([^\]]+)\]")
    for line in lines:
        match = header_re.match(line)
        if match:
            names.append(match.group(1).strip())
    return names


def load_parser(path):
    if os.path.isdir(path):
        raise ValueError("config path is a directory: " + path)
    cp = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cp.read(path, encoding="utf-8-sig")
    except UnicodeDecodeError as ex:
        raise ValueError("cannot decode config %s: %s" % (path, ex))
    except configparser.Error as ex:
        raise ValueError("invalid config: %s" % ex)
    if cp.defaults():
        raise ValueError("[DEFAULT] options are not supported")
    if "server" not in cp:
        raise ValueError("missing [server] section: " + path)
    return cp


def reload_proxy(path, port_override=None):
    cp = load_parser(path)
    admin_key = cp.get("server", "admin_key", fallback="").strip()
    if not admin_key:
        return False, "admin_key not configured"
    host = cp.get("server", "host", fallback="127.0.0.1").split(",")[0].strip()
    host = kiosapi.normalize_host(host)
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    url_host = "[%s]" % host if ":" in host else host
    port = port_override or cp.get("server", "port", fallback="8787")
    url = "http://%s:%s/_admin/reload" % (url_host, port)
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Authorization": "Bearer " + admin_key})
    try:
        with _open_no_redirect(req, 5) as resp:
            return True, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as ex:
        detail = "HTTP %s: %s" % (ex.code, ex.read().decode("utf-8", errors="ignore"))
        ex.close()
        return False, detail
    except Exception as ex:
        return False, type(ex).__name__ + ": " + str(ex)


def cmd_list(args):
    lines = read_lines(args.config)
    cp = load_parser(args.config)
    rows = []
    for name in all_upstreams(lines):
        sec = section_name(name)
        keys = section_keys(lines, sec)
        if name == "default":
            base = cp.get("server", "base_url", fallback="")
        else:
            base = cp.get(sec, "base_url", fallback="")
        rows.append({
            "upstream": name,
            "base_url": base,
            "keys": len(keys),
            "masked": [mask_key(k, args.reveal) for k in keys],
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print("%-16s %-48s %d keys  %s" % (
            row["upstream"], row["base_url"], row["keys"], ", ".join(row["masked"])))
    return 0


def cmd_show(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    cp = load_parser(args.config)
    if name == "default":
        info = {"base_url": cp.get("server", "base_url", fallback="")}
    else:
        info = dict(cp.items(sec)) if cp.has_section(sec) else {}
    info = {k: v for k, v in info.items()
            if k.lower() != "keys" and not k.lower().startswith("key")}
    if args.json:
        payload = {"upstream": name, "keys": [mask_key(k, args.reveal) for k in keys]}
        payload.update(info)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("upstream:", name)
    for field in ("base_url", "prefix", "auth_style", "session_header",
                  "model", "user_agent", "timeout"):
        if field in info:
            print("%-14s %s" % (field + ":", info[field]))
    print("keys (%d):" % len(keys))
    for i, key in enumerate(keys, 1):
        print("  %2d  %s" % (i, mask_key(key, args.reveal)))
    return 0


def cmd_add_keys(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    load_parser(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    added = []
    for key in args.keys:
        key = key.strip()
        if not key:
            continue
        if not valid_key(key):
            print("ERROR: invalid key (whitespace or comma):", mask_key(key))
            return 1
        if key in keys:
            print("skip duplicate:", mask_key(key))
            continue
        keys.append(key)
        added.append(key)
    if not added:
        print("no keys added")
        return 1
    set_section_keys(lines, sec, keys)
    write_lines(args.config, lines)
    print("added %d key(s) to %s: %s" % (
        len(added), name, ", ".join(mask_key(k) for k in added)))
    return finish(args)


def cmd_set_key(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    load_parser(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    index = args.index
    if index < 1 or index > len(keys):
        print("ERROR: index out of range 1..%d" % len(keys))
        return 1
    new_key = args.key.strip()
    if not new_key:
        print("ERROR: empty key")
        return 1
    if not valid_key(new_key):
        print("ERROR: invalid key (whitespace or comma):", mask_key(new_key))
        return 1
    old = keys[index - 1]
    if new_key in keys and new_key != old:
        print("ERROR: key already exists:", mask_key(new_key))
        return 1
    keys[index - 1] = new_key
    set_section_keys(lines, sec, keys)
    write_lines(args.config, lines)
    print("replaced %s key #%d (%s) with %s" % (
        name, index, mask_key(old), mask_key(args.key)))
    return finish(args)


def cmd_remove_key(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    load_parser(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    target = args.target.strip()
    if not target:
        print("ERROR: empty target")
        return 1
    removed = None
    if target.isdigit():
        index = int(target)
        if 1 <= index <= len(keys):
            removed = keys.pop(index - 1)
    else:
        matches = [i for i, key in enumerate(keys)
                   if key == target or key.endswith(target)]
        if len(matches) > 1:
            print("ERROR: target matches %d keys; use remove-keys with --yes"
                  % len(matches))
            return 1
        if matches:
            removed = keys.pop(matches[0])
    if removed is None:
        print("ERROR: no key matched:", target)
        return 1
    set_section_keys(lines, sec, keys)
    write_lines(args.config, lines)
    print("removed 1 key from %s: %s" % (name, mask_key(removed)))
    return finish(args)


def cmd_remove_keys(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    load_parser(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    patterns = [p for p in args.patterns if p]
    if args.ignore_case:
        lowered = [p.lower() for p in patterns]
        matched = [k for k in keys if any(p in k.lower() for p in lowered)]
    else:
        matched = [k for k in keys if any(p in k for p in patterns)]
    if not matched:
        print("no keys matched")
        return 1
    print("matched %d key(s): %s" % (len(matched), ", ".join(mask_key(k) for k in matched)))
    if len(matched) > 1 and not args.yes:
        print("refusing to remove multiple keys without --yes")
        return 1
    remaining = [k for k in keys if k not in matched]
    set_section_keys(lines, sec, remaining)
    write_lines(args.config, lines)
    print("removed %d key(s) from %s" % (len(matched), name))
    return finish(args)


def cmd_clear_keys(args):
    name = normalize_upstream(args.upstream)
    lines = read_lines(args.config)
    load_parser(args.config)
    sec = section_name(name)
    try:
        keys = section_keys(lines, sec)
    except KeyError as ex:
        print("ERROR:", ex)
        return 1
    if not keys:
        print("no keys to clear")
        return 0
    if not args.yes:
        print("would clear %d key(s) from %s; pass --yes" % (len(keys), name))
        return 1
    set_section_keys(lines, sec, [])
    write_lines(args.config, lines)
    print("cleared %d key(s) from %s" % (len(keys), name))
    return finish(args)


def cmd_clear_all_keys(args):
    lines = read_lines(args.config)
    load_parser(args.config)
    names = all_upstreams(lines)
    total = 0
    plan = []
    for name in names:
        sec = section_name(name)
        try:
            keys = section_keys(lines, sec)
        except KeyError:
            continue
        if keys:
            plan.append((name, len(keys)))
            total += len(keys)
    if not total:
        print("no keys to clear")
        return 0
    print("would clear %d key(s): %s" % (
        total, ", ".join("%s=%d" % (n, c) for n, c in plan)))
    if not args.yes:
        print("pass --yes to confirm")
        return 1
    for name in names:
        sec = section_name(name)
        try:
            set_section_keys(lines, sec, [])
        except KeyError:
            continue
    write_lines(args.config, lines)
    print("cleared %d key(s) from %d upstream(s)" % (total, len(plan)))
    return finish(args)


def cmd_state(args):
    cp = load_parser(args.config)
    state_path = kiosapi.resolve_state_path(
        args.config, cp.get("server", "state_file", fallback=""))
    if not state_path:
        print("state persistence disabled")
        return 0
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as ex:
        print("no readable state file at %s (%s)" % (state_path, ex))
        return 0
    if not isinstance(data, dict):
        print("invalid state file at %s (not an object)" % state_path)
        return 1
    saved_all = data.get("upstreams")
    if not isinstance(saved_all, dict):
        saved_all = {}
    lines = read_lines(args.config)
    rows = []
    for name in all_upstreams(lines):
        sec = section_name(name)
        try:
            keys = section_keys(lines, sec)
        except KeyError:
            continue
        by_fp = {kiosapi.key_fingerprint(k): k for k in keys}
        saved = saved_all.get(name, [])
        if not isinstance(saved, list):
            saved = []
        order = []
        for fp in saved:
            if not isinstance(fp, str):
                continue
            key = by_fp.get(fp)
            order.append(mask_key(key) if key else "<removed:%s>" % fp[:8])
        rows.append({"upstream": name, "order": order})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print("%-16s %s" % (row["upstream"], " -> ".join(row["order"])))
    return 0


def cmd_validate(args):
    try:
        cfg = kiosapi.load_config(args.config)
    except kiosapi.ConfigError as ex:
        print("ERROR:", ex)
        return 1
    print("config OK:", args.config)
    print("hosts:", ", ".join(cfg.hosts), "port:", cfg.port)
    print("auth_keys:", len(cfg.auth_keys), "reload_interval:", cfg.reload_interval)
    pools = [cfg.default] + list(cfg.upstreams)
    for upstream in pools:
        print("  %-16s %-48s %d keys" % (
            upstream.name, upstream.base_url, len(upstream.keys)))
    return 0


def _probe(upstream, key, timeout, chat_model=""):
    headers = {"User-Agent": "kiosapi-proxy-check/1.0"}
    style = upstream.auth_style
    if style in ("bearer", "both"):
        headers["Authorization"] = "Bearer " + key
    if style in ("x-api-key", "both"):
        headers["x-api-key"] = key
    if style == "api-key":
        headers["api-key"] = key
    if upstream.session_header:
        headers[upstream.session_header] = "ses_" + "0" * 32
    if chat_model:
        url = kiosapi.join_upstream_url(
            upstream.base_url, "/chat/completions")
        data = json.dumps({
            "model": chat_model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    else:
        url = kiosapi.join_upstream_url(upstream.base_url, "/models")
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, headers=headers)
    try:
        with _open_no_redirect(req, timeout) as resp:
            body = resp.read(65536)
            if chat_model and upstream.error_body_path:
                code = kiosapi.extract_body_error(body, upstream.error_body_path)
                if code is not None:
                    code_text = str(code)
                    if code_text in upstream.error_body_auth_codes:
                        return "AUTH", "body %s" % code_text
                    if code_text in upstream.error_body_rotate_codes:
                        return "LIMIT", "body %s" % code_text
            return "OK", "HTTP %s" % resp.status
    except urllib.error.HTTPError as ex:
        code = ex.code
        try:
            body = ex.read(65536)
        except Exception:
            body = b""
        finally:
            ex.close()
        if code in (402, 429) or kiosapi.is_limit_error(code, body):
            return "LIMIT", "HTTP %s" % code
        if kiosapi.is_auth_error(code, body):
            return "AUTH", "HTTP %s" % code
        return "HTTP %s" % code, "HTTP %s" % code
    except Exception as ex:
        return "ERROR", type(ex).__name__ + ": " + str(ex)


def cmd_check(args):
    cfg = kiosapi.load_config(args.config)
    pools = [cfg.default] + list(cfg.upstreams)
    if args.upstream:
        target = normalize_upstream(args.upstream)
        pools = [u for u in pools if u.name == target]
        if not pools:
            print("ERROR: unknown upstream:", target)
            return 1
    rows = []
    for upstream in pools:
        keys = [key for key in upstream.keys if not kiosapi.is_placeholder_key(key)]
        if not keys:
            rows.append({"upstream": upstream.name, "status": "SKIP",
                         "detail": "no usable key"})
            continue
        result = None
        for key in keys:
            status, detail = _probe(upstream, key, args.timeout, args.chat_model)
            if status == "OK":
                result = (status, detail)
                break
            if result is None or (result[0] == "AUTH" and status != "AUTH"):
                result = (status, detail)
        rows.append({"upstream": upstream.name, "status": result[0],
                     "detail": result[1]})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print("%-16s %-8s %s" % (row["upstream"], row["status"], row["detail"]))
    return 0


def cmd_reload(args):
    ok, detail = reload_proxy(args.config, args.port)
    print(("reload OK: " if ok else "reload failed: ") + detail)
    return 0 if ok else 1


def finish(args):
    if args.no_reload:
        print("saved; reload skipped (--no-reload)")
        return 0
    ok, detail = reload_proxy(args.config, args.port)
    if ok:
        print("reload OK:", detail)
    else:
        print("saved; immediate reload unavailable (%s); file watcher will apply it" % detail)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="kiosapi-proxy key management")
    parser.add_argument("--config", default="proxy.ini", help="ini config file path")
    parser.add_argument("--port", type=int, default=None,
                        help="proxy port for immediate reload (default: config port)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list upstreams and masked keys")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reveal", action="store_true", help="show full keys")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one upstream and its keys")
    p.add_argument("upstream")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reveal", action="store_true", help="show full keys")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("add-key", help="add one key")
    p.add_argument("upstream")
    p.add_argument("key")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=lambda a: cmd_add_keys(argparse.Namespace(
        config=a.config, upstream=a.upstream, keys=[a.key],
        no_reload=a.no_reload, port=a.port)))

    p = sub.add_parser("add-keys", help="add multiple keys")
    p.add_argument("upstream")
    p.add_argument("keys", nargs="+")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_add_keys)

    p = sub.add_parser("set-key", help="replace key by 1-based index")
    p.add_argument("upstream")
    p.add_argument("index", type=int)
    p.add_argument("key")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_set_key)

    p = sub.add_parser("remove-key", help="remove by 1-based index or key tail")
    p.add_argument("upstream")
    p.add_argument("target")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_remove_key)

    p = sub.add_parser("remove-keys", help="remove keys matching substrings")
    p.add_argument("upstream")
    p.add_argument("patterns", nargs="+")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_remove_keys)

    p = sub.add_parser("clear-keys", help="remove all keys from one upstream")
    p.add_argument("upstream")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_clear_keys)

    p = sub.add_parser("clear-all-keys", help="remove all keys from all upstreams")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(func=cmd_clear_all_keys)

    p = sub.add_parser("state", help="show persisted key order")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("validate", help="parse and summarize the config")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check", help="probe upstream /models endpoints")
    p.add_argument("upstream", nargs="?")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--chat-model", default="",
                   help="send a minimal chat completion instead of GET /models")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("reload", help="ask the running proxy to reload now")
    p.set_defaults(func=cmd_reload)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, ValueError, configparser.Error, OSError) as ex:
        print("ERROR:", ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
