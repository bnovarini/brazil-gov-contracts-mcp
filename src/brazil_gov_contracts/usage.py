"""Usage logging: JSON lines with timestamp, tool, client name+version, duration, status."""
import json
import sys
import time
from contextlib import contextmanager

_LOG_PATH = None


def configure(path=None):
    global _LOG_PATH
    _LOG_PATH = path


def _client_info(ctx):
    try:
        params = ctx.request_context.session.client_params
        info = params.clientInfo
        return {"client_name": info.name, "client_version": info.version}
    except Exception:
        return {"client_name": "unknown", "client_version": "unknown"}


@contextmanager
def log_tool(tool, ctx=None):
    rec = {"tool": tool, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if ctx is not None:
        rec.update(_client_info(ctx))
    else:
        rec.update({"client_name": "unknown", "client_version": "unknown"})
    t0 = time.monotonic()
    try:
        yield rec
        rec["status"] = "ok"
    except Exception:
        rec["status"] = "error"
        raise
    finally:
        rec["duration_ms"] = round((time.monotonic() - t0) * 1000)
        line = json.dumps(rec, ensure_ascii=False)
        if _LOG_PATH:
            try:
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                print(line, file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)
