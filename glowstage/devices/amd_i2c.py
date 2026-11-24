"""I2C access to RGB chips on AMD graphics cards through the driver's ADL library.

ADL2_Display_WriteAndReadI2C on the card's OEM line (1) at 100 kHz, 8-bit (shifted)
addresses. ADL always sends iOffset as the first byte, for writes as well as reads, so the
register goes in iOffset and never in the payload (tested on an ASRock RX 7900 XTX:
putting it in the payload too shifts every message by one byte).
"""

from __future__ import annotations

import ctypes
import re
import threading

ADL_OK = 0
ADL_MAX_PATH = 256
ADL_DL_I2C_ACTIONREAD = 0x01
ADL_DL_I2C_ACTIONWRITE = 0x02
I2C_LINE_OEM = 1
I2C_SPEED_KHZ = 100


class ADLI2C(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iLine", ctypes.c_int), ("iAddress", ctypes.c_int),
                ("iOffset", ctypes.c_int), ("iAction", ctypes.c_int), ("iSpeed", ctypes.c_int),
                ("iDataSize", ctypes.c_int), ("pcData", ctypes.c_char_p)]


class AdapterInfo(ctypes.Structure):  # Windows layout
    _fields_ = [("iSize", ctypes.c_int), ("iAdapterIndex", ctypes.c_int),
                ("strUDID", ctypes.c_char * ADL_MAX_PATH), ("iBusNumber", ctypes.c_int),
                ("iDeviceNumber", ctypes.c_int), ("iFunctionNumber", ctypes.c_int), ("iVendorID", ctypes.c_int),
                ("strAdapterName", ctypes.c_char * ADL_MAX_PATH), ("strDisplayName", ctypes.c_char * ADL_MAX_PATH),
                ("iPresent", ctypes.c_int), ("iExist", ctypes.c_int),
                ("strDriverPath", ctypes.c_char * ADL_MAX_PATH), ("strDriverPathExt", ctypes.c_char * ADL_MAX_PATH),
                ("strPNPString", ctypes.c_char * ADL_MAX_PATH), ("iOSDisplayIndex", ctypes.c_int)]


_ALLOC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
_malloc = ctypes.cdll.msvcrt.malloc
_malloc.restype = ctypes.c_void_p
_malloc.argtypes = [ctypes.c_size_t]
_alloc_cb = _ALLOC(lambda size: _malloc(size))  # module-level so it is never garbage collected


class AmdGpu:
    """One AMD graphics card as seen by ADL."""

    def __init__(self, adl: "AdlLibrary", adapter_index: int, name: str, vendor: int, device: int,
                 subvendor: int, subdevice: int):
        self.adl = adl
        self.adapter_index = adapter_index
        self.name = name
        self.vendor, self.device, self.subvendor, self.subdevice = vendor, device, subvendor, subdevice
        self.lock = threading.Lock()

    def _xfer(self, address: int, action: int, offset: int, buf: ctypes.Array) -> int:
        req = ADLI2C(iSize=ctypes.sizeof(ADLI2C), iLine=I2C_LINE_OEM, iAddress=address << 1, iOffset=offset,
                     iAction=action, iSpeed=I2C_SPEED_KHZ, iDataSize=len(buf),
                     pcData=ctypes.cast(buf, ctypes.c_char_p))
        with self.lock:
            return self.adl.write_and_read_i2c(self.adl.context, self.adapter_index, ctypes.byref(req))

    def write_block(self, address: int, register: int, data: bytes) -> None:
        """I2C block write without a count byte: [register][data...]."""
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        rc = self._xfer(address, ADL_DL_I2C_ACTIONWRITE, register, buf)
        if rc != ADL_OK:
            raise OSError(rc, f"I2C write to 0x{address:02x} failed (ADL {rc})")

    def read_block(self, address: int, register: int, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        rc = self._xfer(address, ADL_DL_I2C_ACTIONREAD, register, buf)
        if rc != ADL_OK:
            raise OSError(rc, f"I2C read from 0x{address:02x} failed (ADL {rc})")
        return buf.raw


class AdlLibrary:
    def __init__(self):
        self.dll = ctypes.CDLL("atiadlxx.dll")
        self.context = ctypes.c_void_p()
        create = self.dll.ADL2_Main_Control_Create
        create.argtypes = [_ALLOC, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        if create(_alloc_cb, 1, ctypes.byref(self.context)) != ADL_OK:
            raise OSError(0, "AMD ADL could not start")
        self.write_and_read_i2c = self.dll.ADL2_Display_WriteAndReadI2C
        self.write_and_read_i2c.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ADLI2C)]
        self.write_and_read_i2c.restype = ctypes.c_int

    def gpus(self) -> list[AmdGpu]:
        count = ctypes.c_int()
        self.dll.ADL2_Adapter_NumberOfAdapters_Get(self.context, ctypes.byref(count))
        if count.value <= 0:
            return []
        infos = (AdapterInfo * count.value)()
        if self.dll.ADL2_Adapter_AdapterInfo_Get(self.context, infos, ctypes.sizeof(infos)) != ADL_OK:
            return []
        found, seen_bus = [], set()
        for info in infos:
            pnp = info.strPNPString.decode(errors="replace")
            m = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})&SUBSYS_([0-9A-F]{4})([0-9A-F]{4})", pnp, re.I)
            if not m or info.iBusNumber in seen_bus:
                continue  # ADL lists one entry per display output; keep the first per card
            seen_bus.add(info.iBusNumber)
            vendor, device, subdevice, subvendor = (int(g, 16) for g in m.groups())
            found.append(AmdGpu(self, info.iAdapterIndex, info.strAdapterName.decode(errors="replace").strip(),
                                vendor, device, subvendor, subdevice))
        return found


_library: AdlLibrary | None = None


def amd_gpus() -> list[AmdGpu]:
    """AMD cards with I2C access, or [] when there is no AMD driver."""
    global _library
    try:
        if _library is None:
            _library = AdlLibrary()
        return _library.gpus()
    except OSError:
        return []
