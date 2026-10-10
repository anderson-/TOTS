import os
import socket
import threading

SOCK_PATH = "/tmp/hermes-stream.sock"
_SENTINEL = "__stream_tap_patched__"
_locks = {}
_seqs = {}
_sock = None


def _get_sock():
    global _sock
    if _sock is None:
        _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _sock.setblocking(False)
    return _sock


def _publish(platform, session_id, kind, writer, text):
    try:
        key = f"{session_id}:{writer}"
        with _locks.setdefault(key, threading.Lock()):
            _seqs[key] = _seqs.get(key, 0) + 1
            seq = _seqs[key]
        _get_sock().sendto(
            f"{os.getpid()}\x1f{platform}\x1f{session_id}\x1f{kind}\x1f{writer}\x1f{seq}\x1f{text}".encode("utf-8", "replace"),
            SOCK_PATH,
        )
    except (OSError, BlockingIOError, socket.timeout):
        pass


def _wrap(orig, pub):
    def patched(self, text):
        try:
            if isinstance(text, str) and text:
                pub(self, text)
        except Exception:
            pass
        return orig(self, text)

    patched.__name__ = getattr(orig, "__name__", "delta")
    return patched


def _apply_patch():
    import run_agent

    cls = run_agent.AIAgent
    if getattr(cls, _SENTINEL, False):
        return
    cls._fire_stream_delta = _wrap(
        cls._fire_stream_delta,
        lambda self, t: _publish(
            getattr(self, "platform", "") or "",
            getattr(self, "session_id", "") or "",
            "T", str(id(self)), t))
    cls._fire_reasoning_delta = _wrap(
        cls._fire_reasoning_delta,
        lambda self, t: _publish(
            getattr(self, "platform", "") or "",
            getattr(self, "session_id", "") or "",
            "R", str(id(self)), t))
    setattr(cls, _SENTINEL, True)


def _token(task_id="", tool_call_id="", turn_id="", api_request_id="", **_):
    for name, value in (("tool_call_id", tool_call_id), ("api_request_id", api_request_id),
                        ("task_id", task_id), ("turn_id", turn_id)):
        if value:
            return f"{name}:{value}"
    return ""


def _on_tool(state, tool_name="", session_id="", **kw):
    _publish("", session_id or "", state, _token(**kw), str(tool_name)[:120])


def register(ctx):
    try:
        ctx.register_hook("pre_tool_call", lambda **kw: _on_tool("S", **kw))
        ctx.register_hook("post_tool_call", lambda **kw: _on_tool("E", **kw))
    except Exception:
        pass
    try:
        _apply_patch()
        return
    except Exception:
        pass

    def _patch_on_session(**kw):
        try:
            _apply_patch()
        except Exception:
            pass

    try:
        ctx.register_hook("on_session_start", _patch_on_session)
    except Exception:
        pass
