#!/usr/bin/env python3
"""Canon EOS 5D classic: switch to WinUSB, read shutter count, restore MTP.

Combines the supplied working SetupAPI driver switcher with the DCP/PTP reader.

Requirements:
    64-bit Python on 64-bit Windows
    py -m pip install pyusb libusb-package

Run from an elevated PowerShell/CMD:
    py eos5d_shutter_count_auto.py

No Zadig, wdi-simple.exe, third-party INF, or certificate is required. The
script selects Microsoft's inbox %WINDIR%\\INF\\winusb.inf and removes the
modified device nodes at the end so Windows can bind its normal MTP driver.
"""

from __future__ import annotations

import ctypes as C
import os
import struct
import sys
import time
import traceback
from ctypes import wintypes as W

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1
    import libusb_package
except ImportError as exc:
    raise SystemExit(
        "Missing USB Python package(s). Run:\n"
        "  py -m pip install pyusb libusb-package\n"
        f"Details: {exc}"
    )

# ============================================================================
# Windows SetupAPI driver switching (based on the supplied working script)
# ============================================================================

VID = 0x04A9
EOS5D_PID, DCP_PID = 0x3102, 0x3086

DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x02, 0x04
SPDRP_HARDWAREID, SPDRP_SERVICE = 0x01, 0x04
SPDIT_CLASSDRIVER = 1
DI_ENUMSINGLEINF = 0x00010000
DI_FLAGSEX_ALLOWEXCLUDEDDRVS = 0x00000800
DIIDFLAG_INSTALLNULLDRIVER = 0x00000004
INVALID_HANDLE = C.c_void_p(-1).value
WINUSB_INF = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "INF", "winusb.inf")
PACK = 1 if C.sizeof(C.c_void_p) == 4 else 8


class GUID(C.Structure):
    _fields_ = [
        ("d1", W.DWORD), ("d2", W.WORD), ("d3", W.WORD),
        ("d4", C.c_ubyte * 8),
    ]


class SP_DEVINFO_DATA(C.Structure):
    _pack_ = PACK
    _fields_ = [
        ("cbSize", W.DWORD), ("ClassGuid", GUID), ("DevInst", W.DWORD),
        ("Reserved", C.c_void_p),
    ]


class SP_DEVINSTALL_PARAMS(C.Structure):
    _pack_ = PACK
    _fields_ = [
        ("cbSize", W.DWORD), ("Flags", W.DWORD), ("FlagsEx", W.DWORD),
        ("hwndParent", C.c_void_p), ("InstallMsgHandler", C.c_void_p),
        ("InstallMsgHandlerContext", C.c_void_p), ("FileQueue", C.c_void_p),
        ("ClassInstallReserved", C.c_void_p), ("Reserved", W.DWORD),
        ("DriverPath", C.c_wchar * 260),
    ]


class SP_DRVINFO_DATA(C.Structure):
    _pack_ = PACK
    _fields_ = [
        ("cbSize", W.DWORD), ("DriverType", W.DWORD), ("Reserved", C.c_void_p),
        ("Description", C.c_wchar * 256), ("MfgName", C.c_wchar * 256),
        ("ProviderName", C.c_wchar * 256), ("DriverDate", W.FILETIME),
        ("DriverVersion", C.c_ulonglong),
    ]


def _libs():
    s = C.WinDLL("setupapi", use_last_error=True)
    n = C.WinDLL("newdev", use_last_error=True)
    c = C.WinDLL("cfgmgr32", use_last_error=True)
    P, D, V = C.POINTER, W.DWORD, C.c_void_p

    s.SetupDiGetClassDevsW.restype = V
    s.SetupDiGetClassDevsW.argtypes = [V, W.LPCWSTR, V, D]
    s.SetupDiDestroyDeviceInfoList.argtypes = [V]
    s.SetupDiCreateDeviceInfoList.restype = V
    s.SetupDiCreateDeviceInfoList.argtypes = [V, V]
    s.SetupDiOpenDeviceInfoW.argtypes = [V, W.LPCWSTR, V, D, P(SP_DEVINFO_DATA)]
    s.SetupDiGetDeviceInstanceIdW.argtypes = [V, P(SP_DEVINFO_DATA), W.LPWSTR, D, P(D)]
    s.SetupDiEnumDeviceInfo.argtypes = [V, D, P(SP_DEVINFO_DATA)]
    s.SetupDiGetDeviceRegistryPropertyW.argtypes = [V, P(SP_DEVINFO_DATA), D, P(D), V, D, P(D)]
    s.SetupDiSetDeviceRegistryPropertyW.argtypes = [V, P(SP_DEVINFO_DATA), D, V, D]
    s.SetupDiGetDeviceInstallParamsW.argtypes = [V, P(SP_DEVINFO_DATA), P(SP_DEVINSTALL_PARAMS)]
    s.SetupDiSetDeviceInstallParamsW.argtypes = [V, P(SP_DEVINFO_DATA), P(SP_DEVINSTALL_PARAMS)]
    s.SetupDiBuildDriverInfoList.argtypes = [V, P(SP_DEVINFO_DATA), D]
    s.SetupDiDestroyDriverInfoList.argtypes = [V, P(SP_DEVINFO_DATA), D]
    s.SetupDiEnumDriverInfoW.argtypes = [V, P(SP_DEVINFO_DATA), D, D, P(SP_DRVINFO_DATA)]
    s.SetupDiSetSelectedDriverW.argtypes = [V, P(SP_DEVINFO_DATA), P(SP_DRVINFO_DATA)]
    n.DiInstallDevice.argtypes = [V, V, P(SP_DEVINFO_DATA), P(SP_DRVINFO_DATA), D, P(W.BOOL)]
    n.DiUninstallDevice.argtypes = [V, V, P(SP_DEVINFO_DATA), D, P(W.BOOL)]
    c.CM_Locate_DevNodeW.argtypes = [P(D), W.LPCWSTR, D]
    c.CM_Reenumerate_DevNode.argtypes = [D, D]
    return s, n, c


def is_admin():
    try:
        return bool(C.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fail(what):
    code = C.get_last_error()
    raise RuntimeError(f"{what} failed (Windows error 0x{code:08X}).")


def _prop(s, h, did, prop):
    buf = C.create_unicode_buffer(2048)
    if not s.SetupDiGetDeviceRegistryPropertyW(
        h, C.byref(did), prop, None, buf, C.sizeof(buf), None
    ):
        return ""
    return buf[:].split("\0\0")[0].replace("\0", "\n")


def _find(s, pid, present_only=True):
    """Find the Canon USB device. Can also find a disconnected (phantom) node."""
    flags = DIGCF_ALLCLASSES | (DIGCF_PRESENT if present_only else 0)
    h = s.SetupDiGetClassDevsW(None, "USB", None, flags)
    if h in (None, INVALID_HANDLE):
        _fail("SetupDiGetClassDevs")
    want = f"USB\\VID_{VID:04X}&PID_{pid:04X}"
    i = 0
    while True:
        did = SP_DEVINFO_DATA()
        did.cbSize = C.sizeof(did)
        if not s.SetupDiEnumDeviceInfo(h, i, C.byref(did)):
            s.SetupDiDestroyDeviceInfoList(h)
            return None, None
        if want in _prop(s, h, did, SPDRP_HARDWAREID).upper():
            return h, did
        i += 1


def service(pid):
    """Return driver service for a connected camera; None if not connected."""
    s, _n, _c = _libs()
    h, did = _find(s, pid, present_only=True)
    if h is None:
        return None
    try:
        return _prop(s, h, did, SPDRP_SERVICE)
    finally:
        s.SetupDiDestroyDeviceInfoList(h)


def instance_id(pid):
    """Instance ID of a connected camera, or None."""
    s, _n, _c = _libs()
    h, did = _find(s, pid, present_only=True)
    if h is None:
        return None
    try:
        buf = C.create_unicode_buffer(512)
        if not s.SetupDiGetDeviceInstanceIdW(h, C.byref(did), buf, 512, None):
            _fail("SetupDiGetDeviceInstanceId")
        return buf.value
    finally:
        s.SetupDiDestroyDeviceInfoList(h)


def wait_for_instance(pid, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        inst = instance_id(pid)
        if inst:
            return inst
        time.sleep(0.25)
    return None


def _open(s, dev_instance_id):
    """Open one PnP device node by its instance ID, including a phantom node."""
    h = s.SetupDiCreateDeviceInfoList(None, None)
    if h in (None, INVALID_HANDLE):
        _fail("SetupDiCreateDeviceInfoList")
    did = SP_DEVINFO_DATA()
    did.cbSize = C.sizeof(did)
    if not s.SetupDiOpenDeviceInfoW(h, dev_instance_id, None, 0, C.byref(did)):
        s.SetupDiDestroyDeviceInfoList(h)
        _fail("SetupDiOpenDeviceInfo")
    return h, did


def _winusb_driver(s, h, did, single_inf):
    """Find WinUSB in the driver list; prefer Microsoft's inbox winusb.inf."""
    s.SetupDiDestroyDriverInfoList(h, C.byref(did), SPDIT_CLASSDRIVER)
    params = SP_DEVINSTALL_PARAMS()
    params.cbSize = C.sizeof(params)
    params.FlagsEx = DI_FLAGSEX_ALLOWEXCLUDEDDRVS
    if single_inf:
        params.Flags = DI_ENUMSINGLEINF
        params.DriverPath = WINUSB_INF
    if not s.SetupDiSetDeviceInstallParamsW(h, C.byref(did), C.byref(params)):
        _fail("SetupDiSetDeviceInstallParams")
    if not s.SetupDiBuildDriverInfoList(h, C.byref(did), SPDIT_CLASSDRIVER):
        _fail("SetupDiBuildDriverInfoList")

    found, names, i = None, [], 0
    while True:
        drv = SP_DRVINFO_DATA()
        drv.cbSize = C.sizeof(drv)
        if not s.SetupDiEnumDriverInfoW(h, C.byref(did), SPDIT_CLASSDRIVER, i, C.byref(drv)):
            break
        names.append(drv.Description)
        if found is None and "winusb" in drv.Description.lower().replace(" ", ""):
            found = drv
        i += 1
    where = "winusb.inf" if single_inf else "all class drivers"
    print(f"   {where}: {len(names)} driver(s)" + (f": {', '.join(names[:8])}" if names else ""), flush=True)
    return found


def ensure_winusb(pid):
    """Apply Microsoft's inbox WinUSB driver to this connected camera."""
    if C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Driver installation requires 64-bit Python on 64-bit Windows.")
    s, n, _c = _libs()
    inst = instance_id(pid)
    if inst is None:
        raise RuntimeError(f"Device 04A9:{pid:04X} is not connected.")
    if (service(pid) or "").upper() == "WINUSB":
        print(f"04A9:{pid:04X} already uses WinUSB.", flush=True)
        return False

    print(f"Installing inbox WinUSB on 04A9:{pid:04X} ({inst})...", flush=True)
    touched, h = False, None
    try:
        reboot = W.BOOL()
        h, did = _open(s, inst)
        print("   step 1/2: install null driver", flush=True)
        touched = True
        if not n.DiInstallDevice(None, h, C.byref(did), None,
                                DIIDFLAG_INSTALLNULLDRIVER, C.byref(reboot)):
            _fail("DiInstallDevice (null driver)")
        s.SetupDiDestroyDeviceInfoList(h)
        h, did = _open(s, inst)
        print(f"   device class is now {did.ClassGuid.d1:08X}-{did.ClassGuid.d2:04X}-{did.ClassGuid.d3:04X}", flush=True)

        print("   step 2/2: select WinUSB from Windows winusb.inf", flush=True)
        drv = _winusb_driver(s, h, did, True) or _winusb_driver(s, h, did, False)
        if drv is None:
            raise RuntimeError("Windows offered no WinUSB driver for this device.")
        print(f"   selected: {drv.Description} ({drv.ProviderName})", flush=True)
        if not n.DiInstallDevice(None, h, C.byref(did), C.byref(drv), 0, C.byref(reboot)):
            _fail("DiInstallDevice (WinUSB)")
        touched = False
    finally:
        if h is not None:
            s.SetupDiDestroyDeviceInfoList(h)
        if touched:
            print("   WinUSB installation was incomplete; attempting driver rollback...", flush=True)
            try:
                restore(pid, inst)
            except Exception as exc:
                print(f"   rollback failed: {exc}", flush=True)

    # Windows restarts the device after the driver change; the camera can take
    # several seconds to come back, so poll instead of waiting a fixed time.
    deadline = time.monotonic() + 30
    current = None
    while time.monotonic() < deadline:
        current = service(pid)
        if (current or "").upper() == "WINUSB":
            break
        time.sleep(0.5)
    else:
        state = "device not connected" if current is None else (current or "no driver")
        raise RuntimeError(f"04A9:{pid:04X} did not switch to WinUSB within 30 s ({state}).")
    print(f"   WinUSB active on 04A9:{pid:04X}.", flush=True)
    return True


def restore(pid, preferred_instance=None):
    """Remove the WinUSB-bound devnode so Windows can bind its standard driver.

    preferred_instance is retained from the moment this PID was connected. It
    lets us remove the 3086 (service-mode) devnode after the camera has already
    re-enumerated as 3102 (PTP), when 3086 is no longer physically present.
    """
    s, n, c = _libs()
    was_present = instance_id(pid) is not None
    h = did = None

    if preferred_instance:
        try:
            h, did = _open(s, preferred_instance)
        except RuntimeError:
            h = did = None

    if h is None:
        h, did = _find(s, pid, present_only=was_present)
    if h is None:
        # If no present node was found, try all USB device nodes (including ghosts).
        h, did = _find(s, pid, present_only=False)
    if h is None:
        print(f"04A9:{pid:04X}: no device node to restore.", flush=True)
        return None

    try:
        buf = C.create_unicode_buffer(512)
        node_id = ""
        if s.SetupDiGetDeviceInstanceIdW(h, C.byref(did), buf, 512, None):
            node_id = buf.value
        print(f"Restoring Windows driver for 04A9:{pid:04X}" +
              (f" ({node_id})" if node_id else "") + "...", flush=True)
        reboot = W.BOOL()
        if not n.DiUninstallDevice(None, h, C.byref(did), 0, C.byref(reboot)):
            _fail("DiUninstallDevice")
    finally:
        s.SetupDiDestroyDeviceInfoList(h)

    root = W.DWORD()
    rc = c.CM_Locate_DevNodeW(C.byref(root), None, 0)
    if rc == 0:
        c.CM_Reenumerate_DevNode(root, 0)

    if not was_present:
        print(f"   removed the disconnected 04A9:{pid:04X} device node.", flush=True)
        return "removed"

    deadline = time.monotonic() + 25
    svc = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        svc = service(pid)
        if svc:
            break
    print(f"   driver service after restore: {svc or 'not detected'}", flush=True)
    return svc


# ============================================================================
# Canon EOS 5D PTP / DCP exchange (the supplied working reader)
# ============================================================================

BULK_OUT, INT_IN = 0x02, 0x83
MAGIC = b"EVNTPROCEXE\x00"
REG_PATH = b"SERVER\\1.00\\CAMERA\\PROTO\\SERIAL_NUMBER"
WINDOW_BASE, CHUNK = 0x938, 12
MEMORY_KIND = 2
COUNT_CEILING = 10_000_000   # the original program rejects counts at or above this
REPLY_POLLS = 48             # 48 x 2 s, as in the original program
CLEANUP_POLLS = 3            # after an error: do not wait 96 s per clean-up command
VERBOSE = "-v" in sys.argv


def hx(b):
    return bytes(b).hex(" ")


def handshake_frame():
    return struct.pack("<IIIII", 0, 12, 1, 4, 0)


def register_frames(name):
    """Two frames per service name: registration (160 bytes) and commit (32)."""
    name = name.ljust(12, b"\x00")
    body = struct.pack("<I", 1) + name + struct.pack("<II", 0x84, 0) + REG_PATH.ljust(0x80, b"\x00")
    first = struct.pack("<II", 1, len(body)) + body
    body = struct.pack("<I", 3) + name + struct.pack("<II", 4, 0)
    return first, struct.pack("<II", 1, len(body)) + body


def request(kind, session, seq, payload=b""):
    inner = MAGIC + struct.pack("<IHHHI", 0, kind, session, seq, len(payload)) + payload
    return struct.pack("<II", 2, len(inner)) + inner


def open_session_frame(seq):
    return request(1, 0, seq)


def noarg_frame(session, seq, name):
    return request(5, session, seq, name.encode() + b"\x00" + struct.pack("<II", 0, 0))


def monread_frame(session, seq, address, length):
    p = b"MonReadAndGetData\x00" + struct.pack("<I", 4)
    for v in (length, MEMORY_KIND, address, length):
        p += struct.pack("<IIIII", 2, v, 0, 0, 0)
    return request(7, session, seq, p + struct.pack("<I", 0))


def parse_reply(pkt):
    """Decode an EVNTPROCEXE interrupt packet."""
    if len(pkt) < 0x2C or pkt[0x10:0x1C] != MAGIC:
        return None
    kind, subtype, session, seq, plen = struct.unpack_from("<HHHHI", pkt, 0x20)
    return {
        "type": kind, "subtype": subtype, "session": session, "seq": seq,
        "payload": pkt[0x2C:0x2C + plen],
    }


def decode(window):
    """MECHANICS.md: page = BE16 at 0x943, live = ring[page % 8]."""
    page = int.from_bytes(window[0x0B:0x0D], "big")
    ring = window[0x0D:0x15]
    live = ring[page % 8]
    return page, ring, live, page * 256 + live


# ------------------------------- USB layer ---------------------------------


def backend():
    b = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    if b is None:
        raise RuntimeError("Cannot load libusb-1.0 backend.")
    return b


BACKEND = None


def find(pid):
    return usb.core.find(backend=BACKEND, idVendor=VID, idProduct=pid)


def wait_for_usb(pid, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        dev = find(pid)
        if dev is not None:
            return dev
        time.sleep(0.25)
    return None


def open_claimed(pid, seconds=20):
    """Open the device through libusb and claim interface 0. Right after a driver
    change this can fail for a moment, so keep trying."""
    deadline = time.monotonic() + seconds
    last = "device not found"
    while True:
        dev = find(pid)
        if dev is not None:
            try:
                dev.get_active_configuration()
                usb.util.claim_interface(dev, 0)
                return dev
            except (usb.core.USBError, NotImplementedError) as exc:
                last = str(exc)
                usb.util.dispose_resources(dev)
        if time.monotonic() > deadline:
            raise RuntimeError(f"Cannot open 04A9:{pid:04X} through WinUSB: {last}")
        time.sleep(0.5)


def switch_to_service_mode():
    """Send PTP 0x901F; return after Windows enumerates the 3086 device node."""
    dev = open_claimed(EOS5D_PID)
    print("Sending PTP service-mode command 0x901F...", flush=True)
    try:
        dev.write(0x02, struct.pack("<IHHII", 16, 1, 0x1002, 0, 1), timeout=5000)
        response = bytes(dev.read(0x81, 64, timeout=5000))
        if len(response) < 8:
            raise RuntimeError(f"Short OpenSession response: {hx(response)}")
        code = struct.unpack_from("<H", response, 6)[0]
        if code != 0x2001:
            raise RuntimeError(f"OpenSession failed: 0x{code:04X}")
        try:
            dev.write(0x02, struct.pack("<IHHII", 16, 1, 0x901F, 1, 3), timeout=5000)
            dev.read(0x81, 64, timeout=5000)
        except usb.core.USBError:
            pass  # the camera disconnects during the mode change
    finally:
        usb.util.dispose_resources(dev)

    print("Waiting for service-mode USB ID 04A9:3086...", flush=True)
    inst = wait_for_instance(DCP_PID, 25)
    if inst is None:
        raise RuntimeError("Windows did not detect service-mode device 04A9:3086.")
    return inst


class Dcp:
    def __init__(self, dev):
        self.dev = dev
        self.seq = 0x40
        self.session = 0
        self.polls = REPLY_POLLS

    def status(self):
        st = bytes(self.dev.ctrl_transfer(0xC0, 0, 0, 0, 2, timeout=4000))
        if st != b"\x00\x00":
            raise RuntimeError(f"Camera status {hx(st)} (expected 00 00).")

    def send(self, frame):
        self.dev.ctrl_transfer(0x40, 0, 0x0E, 0, struct.pack("<I", len(frame)), timeout=3000)
        self.status()
        self.dev.write(BULK_OUT, frame, timeout=3000)

    def reply(self, seq, want_type):
        for _ in range(self.polls):
            try:
                pkt = bytes(self.dev.read(INT_IN, 64, timeout=2000))
            except usb.core.USBTimeoutError:
                continue
            if VERBOSE:
                print("   <", hx(pkt), flush=True)
            r = parse_reply(pkt)
            if r and r["seq"] == seq and r["type"] == want_type:
                return r
        raise RuntimeError("The camera did not answer.")

    def connect(self):
        self.dev.ctrl_transfer(0x40, 0, 0x10, 0, struct.pack("<I", 0), timeout=3000)
        self.status()
        self.send(handshake_frame())
        for name in (b"EVNTPROCEXE", b"MEMMNGR"):
            for frame in register_frames(name):
                self.send(frame)
        self.seq += 1
        self.send(open_session_frame(self.seq))
        r = self.reply(self.seq, 1)
        if len(r["payload"]) < 2:
            raise RuntimeError("No session id in the camera's answer.")
        self.session = struct.unpack_from("<H", r["payload"])[0]
        print(f"DCP session opened, id {self.session}.", flush=True)

    def call(self, name):
        self.seq += 1
        self.send(noarg_frame(self.session, self.seq, name))
        r = self.reply(self.seq, 2)
        status_code = int.from_bytes(r["payload"][:4], "little")
        if r["subtype"] != 1 or status_code != 0:
            raise RuntimeError(f"{name}: subtype {r['subtype']}, status 0x{status_code:X}.")

    def read(self, address, length):
        self.seq += 1
        self.send(monread_frame(self.session, self.seq, address, length))
        r = self.reply(self.seq, 2)
        p = r["payload"]
        if r["subtype"] != 3 or len(p) < 8:
            raise RuntimeError(f"Read 0x{address:X}: subtype {r['subtype']}, payload {hx(p)}.")
        status_code, n = struct.unpack_from("<II", p)
        data = p[8:8 + n]
        if status_code != 0 or len(data) != length:
            raise RuntimeError(f"Read 0x{address:X}: status 0x{status_code:X}, {len(data)} bytes.")
        return data

    def back_to_ptp(self):
        self.seq += 1
        try:
            self.send(noarg_frame(self.session, self.seq, "SetUSBToPTPMode"))
            print("Sent SetUSBToPTPMode.", flush=True)
        except usb.core.USBError as exc:
            # The camera may disconnect immediately after accepting this command.
            print(f"USB disconnected while returning to PTP mode: {exc}", flush=True)


# ============================================================================
# Main orchestration
# ============================================================================


def main():
    if os.name != "nt":
        raise RuntimeError("Windows only.")
    if C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Use 64-bit Python on 64-bit Windows.")
    if not is_admin():
        raise RuntimeError(
            "Administrator rights are required. Open PowerShell with 'Run as administrator',\n"
            "then run: py eos5d_shutter_count_auto.py"
        )
    if not os.path.isfile(WINUSB_INF):
        raise RuntimeError(f"Windows inbox WinUSB INF not found: {WINUSB_INF}")

    global BACKEND
    BACKEND = backend()

    print("Canon EOS 5D shutter-count reader with automatic Windows driver switching", flush=True)
    print(f"Python: {C.sizeof(C.c_void_p) * 8}-bit; administrator: yes", flush=True)
    print(f"Inbox driver: {WINUSB_INF}", flush=True)

    dcp_dev = None
    dcp = None
    dcp_instance = None
    normal_instance_before = instance_id(EOS5D_PID)
    window = None
    mon_open = False
    back_attempted = False
    primary_error = None

    try:
        # The normal-mode device must have WinUSB BEFORE PTP 0x901F is sent.
        dcp_instance = instance_id(DCP_PID)
        if dcp_instance:
            print("Camera is already enumerated in service mode (04A9:3086).", flush=True)
            ensure_winusb(DCP_PID)
        else:
            if normal_instance_before is None:
                raise RuntimeError("Canon EOS 5D 04A9:3102 was not found in Windows PnP.")
            ensure_winusb(EOS5D_PID)
            dcp_instance = switch_to_service_mode()
            ensure_winusb(DCP_PID)

        dcp_dev = open_claimed(DCP_PID)
        print("Service mode 04A9:3086 connected through WinUSB.", flush=True)
        dcp = Dcp(dcp_dev)

        dcp.connect()
        dcp.call("MonOpen")
        mon_open = True
        window = dcp.read(WINDOW_BASE, CHUNK) + dcp.read(WINDOW_BASE + CHUNK, CHUNK)
        dcp.call("MonClose")
        mon_open = False
        dcp.back_to_ptp()
        back_attempted = True

    except Exception as exc:
        primary_error = exc
        print(f"\nERROR: {exc}", flush=True)
        if VERBOSE:
            traceback.print_exc()

    finally:
        # Best-effort service cleanup, even if one of the reads failed.
        if dcp is not None and dcp.session and not back_attempted:
            dcp.polls = CLEANUP_POLLS
            if mon_open:
                try:
                    print("Attempting MonClose after an error...", flush=True)
                    dcp.call("MonClose")
                except Exception as exc:
                    print(f"MonClose cleanup failed: {exc}", flush=True)
            try:
                print("Attempting to return camera to PTP mode...", flush=True)
                dcp.back_to_ptp()
                back_attempted = True
            except Exception as exc:
                print(f"SetUSBToPTPMode cleanup failed: {exc}", flush=True)

        if dcp_dev is not None:
            try:
                usb.util.dispose_resources(dcp_dev)
            except Exception as exc:
                print(f"USB cleanup warning: {exc}", flush=True)

        # Give the camera time to re-enumerate as the normal PTP/MTP device.
        print("\nRestoring standard Windows drivers...", flush=True)
        normal_now = wait_for_instance(EOS5D_PID, 15)
        if normal_now:
            print("Normal camera ID 04A9:3102 detected again.", flush=True)
        elif back_attempted:
            print("WARNING: camera did not reappear as 04A9:3102.", flush=True)

        # Remove the service-mode devnode even though it has usually disappeared.
        try:
            restore(DCP_PID, dcp_instance)
        except Exception as exc:
            print(f"Could not restore service-mode device node: {exc}", flush=True)

        # Re-fetch the current normal-mode instance ID after the PnP re-enumeration.
        normal_now = instance_id(EOS5D_PID) or normal_now or normal_instance_before
        if normal_now:
            try:
                driver_after = restore(EOS5D_PID, normal_now)
                if driver_after:
                    print(f"Normal-mode driver: {driver_after}", flush=True)
            except Exception as exc:
                print(f"Could not restore normal-mode driver: {exc}", flush=True)
        else:
            print("No 04A9:3102 PnP node available to restore right now.", flush=True)

    if primary_error is not None:
        raise primary_error

    if window is None:
        raise RuntimeError("No shutter-counter data was read.")

    page, ring, live, count = decode(window)
    print("\nRaw window 0x938..0x94F:", hx(window))
    print(f"page = {page}, slot = {page % 8}, live = 0x{live:02X}, ring = {hx(ring)}")
    if window in (b"\xff" * len(window), b"\x00" * len(window)):
        raise RuntimeError("The counter memory reads as blank (all FF or all 00); "
                           "no shutter count can be derived from it.")
    if count >= COUNT_CEILING:
        raise RuntimeError(f"Decoded {count}, which is not a believable shutter count "
                           f"(limit {COUNT_CEILING}); the data above is shown for reference.")
    print(f"\nSHUTTER COUNT: {count}")
    if live == 0xFF:
        print("(live byte is 0xFF: accurate to within 255)")

    final_service = service(EOS5D_PID)
    if final_service:
        print(f"\nWindows driver restored: {final_service}")
    else:
        print("\nCamera driver was reset; Windows may need a moment to finish MTP detection.")


def _run():
    try:
        main()
        return 0
    except Exception as exc:
        print(f"\nFATAL: {exc}", flush=True)
        if VERBOSE:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(_run())
