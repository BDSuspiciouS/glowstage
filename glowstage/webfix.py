"""Fixes for pywebview's WebView2 window (pywebview 6.x), and a watchdog for when the app hangs.

1. pywebview answers every call the page makes (get_state, get_live, ...) by running a bit of
   JavaScript in the page and waiting for WebView2 to say it ran, without a time limit. When that
   word never comes (the page is reloading, its renderer crashed, the window is closing, the
   window's thread is stuck), the thread waits forever. The page asks again a moment later, and
   the waiting threads pile up until the app freezes. Here it waits at most EVAL_TIMEOUT seconds,
   without blocking on the window's thread either.
2. When WebView2's page process crashes or stops responding, the window stays white or frozen.
   Here the page reloads by itself.
3. watch(): a black box. Every few seconds it checks that the window still answers; when it
   doesn't for HANG_AFTER seconds (or a call from the page runs that long), it writes down where
   every thread of the app is (hang.log), so a freeze can be found and fixed. Native crashes go
   to crash.log (faulthandler).
"""

from __future__ import annotations

import faulthandler
import json
import logging
import sys
import threading
import time
import traceback

from .paths import APP_DIR

log = logging.getLogger(__name__)

EVAL_TIMEOUT = 10.0    # seconds a call's answer may take to reach the page
HANG_AFTER = 15.0      # seconds without an answer from the window before the black box writes
_busy: dict[int, tuple[str, float]] = {}   # thread id -> (page call, since when): see track()
_busy_lock = threading.Lock()


def install() -> None:
    """Patch pywebview's WebView2 backend (before webview.start())."""
    try:
        from webview.platforms import edgechromium as ec
        from System import Action, String                          # (pythonnet, loaded by pywebview)
        from System.Threading.Tasks import Task
    except Exception as e:   # another pywebview, or no WebView2: run as it is
        log.warning("Window fixes not installed: %s", e)
        return

    def evaluate_js(self, script, parse_json):
        done = threading.Event()
        box: dict = {}

        def ran(task):
            try:
                res = json.loads(task.Result)
                if parse_json and res is not None:
                    try:
                        res = json.loads(res)
                    except Exception:
                        pass
                box["result"] = res
            except Exception as e:      # the script failed, or the page went away meanwhile
                box["error"] = e
            finally:
                done.set()

        def start():
            self.webview.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](ran), self.syncContextTaskScheduler)

        try:
            # BeginInvoke: never wait for the window's thread itself, only (at most EVAL_TIMEOUT) for the answer.
            self.webview.BeginInvoke(Action(start))
        except Exception as e:
            log.debug("Could not run script in the window: %s", e)
            return None
        if not done.wait(EVAL_TIMEOUT):
            log.warning("The window didn't answer within %.0f s", EVAL_TIMEOUT)
        return box.get("result")

    ec.EdgeChrome.evaluate_js = evaluate_js

    ready = ec.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        ready(self, sender, args)
        if not args.IsSuccess:
            return
        try:
            sender.CoreWebView2.ProcessFailed += lambda s, e: _process_failed(s, e)
        except Exception as e:
            log.debug("Could not watch the window's page process: %s", e)

    ec.EdgeChrome.on_webview_ready = on_webview_ready


def _process_failed(core, event) -> None:
    kind = str(event.ProcessFailedKind)
    log.warning("The window's page process failed (%s)", kind)
    if "Render" in kind or "Frame" in kind:
        try:
            core.Reload()
            log.info("Reloaded the window's page")
        except Exception as e:
            log.warning("Could not reload the window's page: %s", e)


# ---- the black box --------------------------------------------------------------------------

def enable_crash_log() -> None:
    """Native crashes (a driver, a DLL) write where every thread was to crash.log."""
    try:
        f = open(APP_DIR / "crash.log", "a", encoding="utf-8")
        f.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
    except OSError as e:
        log.warning("No crash log: %s", e)


SLOW = 3.0      # seconds: a page call that takes this long gets where it waits written to the log
WAITS_FOR_YOU = {"export_settings", "pick_settings_file"}   # calls that wait for a file dialog: not slow


def _slow_calls() -> None:
    """Twice a second: a call from the page running SLOW seconds or more gets the place it's
    waiting at written to the log, once, so a slow window can be traced to its cause."""
    seen: set[tuple[int, float]] = set()
    while True:
        time.sleep(0.5)
        now = time.monotonic()
        with _busy_lock:
            slow = [(tid, name, since) for tid, (name, since) in _busy.items() if now - since >= SLOW]
        frames = sys._current_frames() if slow else {}
        for tid, name, since in slow:
            if (tid, since) in seen or tid not in frames:
                continue
            seen.add((tid, since))
            where = "".join(traceback.format_stack(frames[tid])[-12:])
            log.warning("Window call %s is slow (%.1f s so far); it waits here:\n%s", name, now - since, where.rstrip())
        if len(seen) > 200:
            seen.clear()


_sampler: threading.Thread | None = None


class track:
    """`with track("get_state"):` marks a call from the page, so the watchdog can tell which one
    is stuck, and slow calls are logged (with where they wait: _slow_calls)."""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        global _sampler
        if self.name in WAITS_FOR_YOU:
            return
        with _busy_lock:
            if _sampler is None:
                _sampler = threading.Thread(target=_slow_calls, name="slow-calls", daemon=True)
                _sampler.start()
            _busy[threading.get_ident()] = (self.name, time.monotonic())

    def __exit__(self, *exc):
        with _busy_lock:
            name, since = _busy.pop(threading.get_ident(), (self.name, time.monotonic()))
        took = time.monotonic() - since
        if took > 2.0:
            log.warning("Window call %s took %.1f s", name, took)
        return False


def watch(window, stop: threading.Event) -> None:
    """Runs on its own thread for as long as the app runs."""
    last_dump = 0.0
    health_at = time.monotonic() + 600
    while not stop.wait(5.0):
        t0 = time.monotonic()
        answered = window.evaluate_js("1") == 1 if not stop.is_set() else True
        took = time.monotonic() - t0
        with _busy_lock:
            stuck = [(n, time.monotonic() - s) for n, s in _busy.values() if time.monotonic() - s > HANG_AFTER]
        if (not answered and took >= EVAL_TIMEOUT or stuck) and time.monotonic() - last_dump > 600:
            last_dump = time.monotonic()
            reason = f"window didn't answer for {took:.0f} s" if not answered else \
                ", ".join(f"{n} running {s:.0f} s" for n, s in stuck)
            _dump(reason)
        if time.monotonic() > health_at:
            health_at = time.monotonic() + 600
            log.info("Health: %d threads", threading.active_count())


def _dump(reason: str) -> None:
    log.warning("App looks stuck (%s): writing hang.log", reason)
    try:
        with open(APP_DIR / "hang.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}: {reason}, {threading.active_count()} threads\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except OSError as e:
        log.warning("Could not write hang.log: %s", e)
