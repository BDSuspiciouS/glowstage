"""Small Windows HID helpers using only the system DLLs: list HID interfaces, open one,
write output reports and read input reports with a timeout."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes as wt

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
INVALID_HANDLE = wt.HANDLE(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wt.DWORD),
                ("Reserved", ctypes.c_size_t)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17), ("Counts", wt.USHORT * 10)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                                      ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CreateEventW.restype = wt.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
for _fn in (k32.WriteFile, k32.ReadFile):
    _fn.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.CancelIo.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_FlushQueue.argtypes = [wt.HANDLE]
hid.HidD_GetProductString.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]


def hid_paths() -> list[str]:
    """Device paths of every present HID interface."""
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if devs == INVALID_HANDLE:
        return []
    paths = []
    try:
        index = 0
        iface = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
        while setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), index, ctypes.byref(iface)):
            index += 1
            size = wt.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), None, 0, ctypes.byref(size), None)
            buf = ctypes.create_string_buffer(size.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), buf, size, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return paths


def _caps(handle) -> HIDP_CAPS:
    caps = HIDP_CAPS()
    pre = ctypes.c_void_p()
    if hid.HidD_GetPreparsedData(handle, ctypes.byref(pre)):
        hid.HidP_GetCaps(pre, ctypes.byref(caps))
        hid.HidD_FreePreparsedData(pre)
    return caps


def hid_interfaces() -> list[dict]:
    """path, vid, pid, usage_page, usage and product name of every HID interface (sends nothing)."""
    out = []
    for path in hid_paths():
        m = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", path, re.I)
        if not m:
            continue
        handle = k32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if handle in (None, INVALID_HANDLE):
            continue
        try:
            caps = _caps(handle)
            name = ctypes.create_unicode_buffer(128)
            hid.HidD_GetProductString(handle, name, ctypes.sizeof(name))
        finally:
            k32.CloseHandle(handle)
        out.append({"path": path, "vid": int(m.group(1), 16), "pid": int(m.group(2), 16),
                    "usage_page": caps.UsagePage, "usage": caps.Usage, "product": name.value.strip()})
    return out


class HidDevice:
    """One opened HID interface. Reports are written with a leading report id of 0."""

    def __init__(self, path: str):
        self.path = path
        self.handle = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle in (None, INVALID_HANDLE):
            raise OSError(ctypes.get_last_error(), "Could not open the HID device")
        caps = _caps(self.handle)
        self.out_len = caps.OutputReportByteLength
        self.in_len = caps.InputReportByteLength

    def close(self) -> None:
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def flush(self) -> None:
        """Drop input reports that are waiting (e.g. echoes we don't need)."""
        hid.HidD_FlushQueue(self.handle)

    def _io(self, fn, buf, length: int, timeout_ms: int) -> int:
        ov = OVERLAPPED(hEvent=k32.CreateEventW(None, True, False, None))
        try:
            if not fn(self.handle, buf, length, None, ctypes.byref(ov)):
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    raise OSError(err, "HID transfer failed")
                if k32.WaitForSingleObject(ov.hEvent, timeout_ms) != 0:
                    k32.CancelIo(self.handle)
                    raise TimeoutError("The device did not answer")
            done = wt.DWORD()
            k32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(done), True)
            return done.value
        finally:
            k32.CloseHandle(ov.hEvent)

    def write(self, payload: bytes, timeout_ms: int = 1000) -> None:
        out = bytearray(self.out_len)
        out[1:1 + len(payload)] = payload
        self._io(k32.WriteFile, (ctypes.c_char * len(out)).from_buffer(out), len(out), timeout_ms)

    def read(self, timeout_ms: int) -> bytes | None:
        """One input report without its report id, or None if nothing arrives in time."""
        buf = (ctypes.c_char * self.in_len)()
        try:
            n = self._io(k32.ReadFile, buf, self.in_len, timeout_ms)
        except TimeoutError:
            return None
        return bytes(buf)[1:n]
