"""Windows integration: start with Windows, single instance, taskbar identity, starting
OpenRGB as administrator without a prompt, and reacting to shutdown."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from ctypes import wintypes as wt
from pathlib import Path
from xml.sax.saxutils import escape

from .paths import APP_DIR, APP_ID, APP_NAME, FROZEN, INSTALL_DIR, LEGACY_APP_DIR, RES_DIR, launch_command

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME
LEGACY_RUN_NAME = "RGBControl"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def autostart_command() -> str:
    return subprocess.list2cmdline(launch_command("--minimized", "--autostart"))


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


_instance_handles: list = []


def claim_single_instance(on_show) -> bool:
    """Return True if this is the first instance. Otherwise ask the running one to show itself.

    Uses a named mutex and event, not a TCP port: on PCs with Hyper-V or WSL every port from
    1024 up can be handed out to other programs, and a busy port made the app quit silently."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateEventW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    k32.SetEvent.argtypes = [wt.HANDLE]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    mutex = k32.CreateMutexW(None, False, f"Local\\{APP_NAME}.SingleInstance")
    already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    show = k32.CreateEventW(None, False, False, f"Local\\{APP_NAME}.Show")
    if already_running:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # let the running instance take focus
        k32.SetEvent(show)
        return False
    _instance_handles.extend([mutex, show])  # keep them open for the life of the process

    def serve():
        while k32.WaitForSingleObject(show, 0xFFFFFFFF) == 0:
            on_show()

    threading.Thread(target=serve, name="instance", daemon=True).start()
    return True


def wait_for_exit(pid: int, timeout: float) -> None:
    """Block until process `pid` has ended (or timeout), e.g. the copy we are replacing."""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, int(timeout * 1000))
        k32.CloseHandle(handle)


def set_app_id() -> None:
    """Group the window under its own taskbar icon instead of python.exe's."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


# ---- moving over from the app's old name ------------------------------------------------------

def migrate_legacy() -> None:
    """The app used to be called "RGB Control": carry its settings over (lights, presets, names,
    zone sizes), move "start with Windows" to the new name and replace the old Start menu entry.
    Runs at every start and does nothing once there is nothing left to move."""
    if LEGACY_APP_DIR.is_dir() and not (APP_DIR / "config.json").exists():
        try:
            shutil.copytree(LEGACY_APP_DIR, APP_DIR, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("webview", "*.log*", "shutdown-trace.txt"))
            log.info("Moved the settings over from %s", LEGACY_APP_DIR)
        except OSError as e:
            log.warning("Could not move the old settings over: %s", e)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            winreg.QueryValueEx(key, LEGACY_RUN_NAME)
            winreg.DeleteValue(key, LEGACY_RUN_NAME)
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            log.info("Moved 'start with Windows' over to the new name")
    except OSError:
        pass
    old_lnk = START_MENU / "RGB Control.lnk"
    if old_lnk.exists() and make_shortcut():
        old_lnk.unlink(missing_ok=True)
        log.info("Replaced the old Start menu entry")


START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def make_shortcut() -> bool:
    """Put the app in the Start menu (so Windows Search finds it), with its icon and app id."""
    script = RES_DIR / "tools" / "install-shortcut.ps1"
    if not script.exists():
        return False
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if FROZEN:
        args += ["-Exe", sys.executable]
    r = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        log.warning("Could not add the Start menu entry: %s", (r.stderr or r.stdout).strip()[:300])
    return r.returncode == 0


def ensure_shortcut() -> None:
    """The installed app adds itself to the Start menu on its first start (and after a move)."""
    lnk = START_MENU / f"{APP_NAME}.lnk"
    marker = APP_DIR / "shortcut.txt"
    here = sys.executable if FROZEN else str(INSTALL_DIR)
    if FROZEN and (not lnk.exists() or not marker.exists() or marker.read_text(encoding="utf-8") != here):
        if make_shortcut():
            marker.write_text(here, encoding="utf-8")


# ---- OpenRGB as administrator, without a UAC prompt every time ------------------------------
# A scheduled task with "run with highest privileges" is registered once (one UAC approval).
# The signed-in user can then start and stop it from a normal process, no prompt needed.

_TASK_NAMES = (rf"{APP_NAME}\OpenRGB", r"RGB Control\OpenRGB")   # (new, from before the rename)


def _task_xml(name: str) -> str | None:
    r = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True,
                       text=True, creationflags=NO_WINDOW)
    return r.stdout if r.returncode == 0 else None


def openrgb_task() -> str:
    """The registered task's name: the one from before the rename keeps working, so existing
    installs don't get a new UAC prompt."""
    for name in _TASK_NAMES:
        if _task_xml(name) is not None:
            return name
    return _TASK_NAMES[0]


def openrgb_task_command() -> str | None:
    """The command the registered task runs, or None if there is no task."""
    xml = _task_xml(openrgb_task())
    if xml is None:
        return None
    start = xml.find("<Command>")
    end = xml.find("</Command>")
    return xml[start + 9:end] if start >= 0 and end > start else ""


def create_openrgb_task(exe: Path, args: list[str], timeout: float = 90.0) -> bool:
    """Register the task (Windows asks for approval once). True when it exists afterwards."""
    params = f'/Create /TN "{openrgb_task()}" /XML "{write_openrgb_task_xml(exe, args)}" /F'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "schtasks.exe", params, None, 0)
    if rc <= 32:
        return False  # approval was declined
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if openrgb_task_command() is not None:
            return True
        time.sleep(0.5)
    return False


def write_openrgb_task_xml(exe: Path, args: list[str]) -> Path:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    arguments = " ".join(f'"{a}"' if " " in a else a for a in args)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts OpenRGB with administrator rights for {APP_NAME} (needed for RAM lighting).</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(exe))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(exe.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    path = Path(tempfile.gettempdir()) / "glowstage_openrgb_task.xml"
    path.write_text(xml, encoding="utf-16")
    return path


def run_openrgb_task() -> bool:
    r = subprocess.run(["schtasks", "/Run", "/TN", openrgb_task()], capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def end_openrgb_task() -> bool:
    r = subprocess.run(["schtasks", "/End", "/TN", openrgb_task()], capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


# ---- shutdown, lock, screen off, sleep -------------------------------------------------------

SM_SHUTTINGDOWN = 0x2000
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x4, 0x7, 0x12
PBT_POWERSETTINGCHANGE = 0x8013
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID), ("DataLength", wt.DWORD), ("Data", wt.DWORD)]


# The screen: 0 off, 1 on, 2 dimmed. Windows says what it is right away, then every change.
GUID_CONSOLE_DISPLAY_STATE = GUID(0x6FE69556, 0x704A, 0x47A0,
                                  (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))


def shutting_down() -> bool:
    return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))


def watch_windows(on_shutdown, on_away=None) -> None:
    """Call on_shutdown() when Windows asks to shut down, restart or sign out; it runs on the
    watcher thread and should finish within a couple of seconds. And on_away(reason, on) when
    the PC is locked or unlocked ("locked"), its screen goes off or on ("screen off"), or it goes
    to sleep or wakes up ("asleep"); before sleep it has about two seconds."""
    # Our own copies of the DLLs, so these prototypes never clash with another module's.
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    # Be asked before most other programs, so OpenRGB is still running when we switch lights off.
    ctypes.windll.kernel32.SetProcessShutdownParameters(0x3FF, 0)
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wt.ATOM
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.RegisterPowerSettingNotification.argtypes = [wt.HANDLE, ctypes.POINTER(GUID), wt.DWORD]
    user32.RegisterPowerSettingNotification.restype = wt.HANDLE
    handled = threading.Event()

    def away(reason: str, on: bool) -> None:
        try:
            on_away(reason, on)
        except Exception:
            log.exception("away handler failed")

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            if not handled.is_set():
                handled.set()
                try:
                    on_shutdown()
                except Exception:
                    log.exception("shutdown handler failed")
            return 1  # never block shutdown
        if msg == WM_ENDSESSION:
            return 0
        if msg == WM_WTSSESSION_CHANGE and on_away:
            if wparam in (WTS_SESSION_LOCK, WTS_SESSION_UNLOCK):
                away("locked", wparam == WTS_SESSION_LOCK)
            return 0
        if msg == WM_POWERBROADCAST and on_away:
            if wparam == PBT_APMSUSPEND:
                away("asleep", True)
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                away("asleep", False)
            elif wparam == PBT_POWERSETTINGCHANGE and lparam:
                setting = POWERBROADCAST_SETTING.from_address(lparam)
                if bytes(setting.PowerSetting) == bytes(GUID_CONSOLE_DISPLAY_STATE):
                    away("screen off", setting.Data == 0)
            return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def listen(hwnd) -> None:
        """Ask for lock and screen news. Right after Windows starts, the session service may
        not be ready yet: then try again for a minute."""
        if not user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(GUID_CONSOLE_DISPLAY_STATE), 0):
            log.warning("Can't follow the screen going off")
        wtsapi32 = ctypes.WinDLL("wtsapi32")
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]
        wtsapi32.WTSRegisterSessionNotification.restype = wt.BOOL
        for _ in range(12):
            if wtsapi32.WTSRegisterSessionNotification(hwnd, 0):   # NOTIFY_FOR_THIS_SESSION
                return
            time.sleep(5)
        log.warning("Can't follow the PC being locked")

    def run():
        proc = WNDPROC(wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName=f"{APP_NAME}ShutdownWatcher")
        user32.RegisterClassW(ctypes.byref(cls))
        # A hidden top-level window: message-only windows don't get WM_QUERYENDSESSION.
        hwnd = user32.CreateWindowExW(0, cls.lpszClassName, f"{APP_NAME} shutdown watcher", 0,
                                      0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            log.warning("shutdown watcher window could not be created")
            return
        if on_away:
            threading.Thread(target=listen, args=(hwnd,), name="away-listen", daemon=True).start()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        del proc

    threading.Thread(target=run, name="shutdown-watcher", daemon=True).start()
