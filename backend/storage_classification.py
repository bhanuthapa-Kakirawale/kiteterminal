"""Where does a path physically live? The fixed-local-drive check for the maintenance commands that write the
database (V12 final readiness, Phase B), and the failure-domain facts the off-machine backup check needs (Phase D).

Read-only: nothing here opens a database, writes a file or touches the network. A UNC path is classified from its
text alone, BEFORE any filesystem call, so a share is never contacted. Every Windows query goes through an adapter
(WindowsStorageAdapter; tests inject fakes), and any query that fails, or any answer that is not understood, gives
UNKNOWN: refused, never guessed at.

Categories (only FIXED_LOCAL is acceptable for a database):
    FIXED_LOCAL        an internal fixed disk volume (NTFS / ReFS, a non-removable bus)
    REMOVABLE          a removable drive, or a USB / FireWire / SD / MMC disk even when Windows reports it as fixed
    NETWORK_UNC        a \\\\server\\share path (also \\\\?\\UNC\\..., and a junction, symlink or subst onto one)
    NETWORK_MAPPED     a drive letter mapped to a network share (a drive letter does not make it local)
    NETWORK_BLOCK      a network block device (iSCSI): local-looking, remote storage
    CLOUD_SYNCED       inside a OneDrive / Dropbox folder, or a cloud-placeholder file, or a cloud virtual drive
    OPTICAL_OR_RAM     a CD/DVD drive or a RAM disk
    VIRTUAL_DISK       a mounted VHD/VHDX: the backing file could be anywhere
    UNAVAILABLE        the drive does not exist or has no medium
    INVALID_PATH       not a usable file system path (empty, NUL, reserved characters, device namespace)
    UNKNOWN            the storage could not be established (a query failed or gave an unknown answer)

Limits (documented in output/audits/.../IMPLEMENTATION_REPORT.md): the classification is what Windows reports.
A network share presented as a local disk by a driver (some VPN / virtual-disk products) or a cloud client that
uses none of the markers below cannot be told apart from a fixed disk; a bus type of 0 (Unknown) is refused.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field

FIXED_LOCAL = "FIXED_LOCAL"
REMOVABLE = "REMOVABLE"
NETWORK_UNC = "NETWORK_UNC"
NETWORK_MAPPED = "NETWORK_MAPPED"
NETWORK_BLOCK = "NETWORK_BLOCK"
CLOUD_SYNCED = "CLOUD_SYNCED"
OPTICAL_OR_RAM = "OPTICAL_OR_RAM"
VIRTUAL_DISK = "VIRTUAL_DISK"
UNAVAILABLE = "UNAVAILABLE"
INVALID_PATH = "INVALID_PATH"
UNKNOWN = "UNKNOWN"

NETWORK_CATEGORIES = frozenset({NETWORK_UNC, NETWORK_MAPPED, NETWORK_BLOCK})

# GetDriveTypeW
DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = range(7)
DRIVE_TYPE_NAMES = {0: "unknown", 1: "no root directory", 2: "removable", 3: "fixed", 4: "network (remote)",
                    5: "CD-ROM", 6: "RAM disk"}

# STORAGE_BUS_TYPE
BUS_TYPE_NAMES = {0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre", 7: "USB",
                  8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 14: "Virtual",
                  15: "FileBackedVirtual", 16: "Spaces", 17: "NVMe", 18: "SCM", 19: "UFS", 20: "NVMeoF"}
REMOVABLE_BUSES = frozenset({4, 7, 12, 13})
NETWORK_BUSES = frozenset({9, 20})
VIRTUAL_BUSES = frozenset({14, 15})
LOCAL_BUSES = frozenset({1, 2, 3, 5, 6, 8, 10, 11, 16, 17, 18, 19})

ACCEPTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
CLOUD_FILESYSTEMS = frozenset({"DRIVEFS", "GOOGLEDRIVEFS", "CSCFS"})

# File attributes / reparse tags that mark a cloud placeholder (OneDrive Files On-Demand and the Cloud Files API).
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_PINNED = 0x80000
FILE_ATTRIBUTE_UNPINNED = 0x100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ATTRIBUTES = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_PINNED
                    | FILE_ATTRIBUTE_UNPINNED | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
IO_REPARSE_TAG_CLOUD_MASK = 0xFFFF0FFF
IO_REPARSE_TAG_CLOUD = 0x9000001A

_RESERVED_CHARS = set('<>"|?*')
_MAX_DEPTH = 6

REMEDIATION = {
    REMOVABLE: "Move the database to an internal fixed disk (for example C: or D: on this machine's own SSD) and "
               "point the command at it with --db; never run the terminal from a removable or USB disk.",
    NETWORK_UNC: "Copy the database to an internal fixed local disk and run the command there; SQLite locking is "
                 "not reliable over a network share.",
    NETWORK_MAPPED: "This drive letter is a mapped network share. Copy the database to an internal fixed local disk "
                    "and run the command there.",
    NETWORK_BLOCK: "This volume is network block storage (iSCSI). Use an internal fixed local disk.",
    CLOUD_SYNCED: "Move the database out of the cloud-synced folder (OneDrive / Dropbox / Google Drive) to a plain "
                  "local folder on an internal disk; the sync client can copy or replace the file underneath SQLite.",
    OPTICAL_OR_RAM: "Use an internal fixed disk; optical media and RAM disks are not durable storage for the database.",
    VIRTUAL_DISK: "The volume is a mounted virtual disk whose backing file may be anywhere. Use an internal fixed disk.",
    UNAVAILABLE: "The drive does not exist or has no medium. Check the --db path.",
    INVALID_PATH: "Pass a plain file path such as D:\\Claude_WS\\kite-terminal\\backend\\kite_terminal.db.",
    UNKNOWN: "The storage type could not be established, so the command refuses. Run it on an internal fixed "
             "NTFS disk; if this is one, record the reason above and report it (do not bypass the check).",
}


@dataclass(frozen=True)
class StorageVerdict:
    category: str
    reason: str
    path: str
    canonical_path: str | None = None
    volume_root: str | None = None
    drive_type: str | None = None
    filesystem: str | None = None
    bus_type: str | None = None
    physical_disks: tuple = ()
    loopback: bool = False
    details: dict = field(default_factory=dict)

    @property
    def acceptable_for_database(self) -> bool:
        return self.category == FIXED_LOCAL

    @property
    def remediation(self) -> str:
        return "" if self.acceptable_for_database else REMEDIATION.get(self.category, REMEDIATION[UNKNOWN])

    def refusal(self) -> str | None:
        """None when the path may hold the database; otherwise the operator-facing refusal (reason + what to do)."""
        if self.acceptable_for_database:
            return None
        return (f"STORAGE_NOT_FIXED_LOCAL: {self.category}: {self.reason}. Nothing was opened or changed. "
                f"{self.remediation}")

    def as_record(self) -> dict:
        """Safe to log and to put in an evidence file: storage facts only, no file contents."""
        return {"category": self.category, "acceptable_for_database": self.acceptable_for_database,
                "reason": self.reason, "canonical_path": self.canonical_path, "volume_root": self.volume_root,
                "drive_type": self.drive_type, "filesystem": self.filesystem, "bus_type": self.bus_type,
                "physical_disks": list(self.physical_disks), "loopback": self.loopback,
                "details": dict(self.details)}


# ---- the Windows adapter (stdlib ctypes only) --------------------------------------------------------------------

class WindowsStorageAdapter:
    """Every OS query the classifier makes. Each method either answers or raises; the classifier turns a raise
    into UNKNOWN. Volume handles are opened with zero access rights (no administrator needed, nothing read)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes, self._wt = ctypes, wintypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mpr = ctypes.WinDLL("mpr", use_last_error=True)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def drive_type(self, root: str) -> int:
        return int(self._k32.GetDriveTypeW(self._ctypes.c_wchar_p(root)))

    def query_dos_device(self, drive: str) -> str | None:
        buf = self._ctypes.create_unicode_buffer(4096)
        n = self._k32.QueryDosDeviceW(self._ctypes.c_wchar_p(drive), buf, len(buf))
        if not n:
            raise OSError(self._ctypes.get_last_error(), f"QueryDosDeviceW({drive}) failed")
        return buf.value

    def mapped_connection(self, drive: str) -> str | None:
        """The remote name of a mapped drive letter, '' when mapped but unavailable, None when not mapped."""
        buf = self._ctypes.create_unicode_buffer(1024)
        size = self._wt.DWORD(len(buf))
        rc = self._mpr.WNetGetConnectionW(self._ctypes.c_wchar_p(drive), buf, self._ctypes.byref(size))
        if rc == 0:
            return buf.value
        if rc in (2250, 1200):          # ERROR_NOT_CONNECTED, ERROR_BAD_DEVICE: not a network drive
            return None
        if rc in (1201, 1222, 53):      # ERROR_CONNECTION_UNAVAIL, ERROR_NO_NETWORK, ERROR_BAD_NETPATH: mapped
            return ""
        raise OSError(rc, f"WNetGetConnectionW({drive}) failed")

    def volume_path_name(self, path: str) -> str:
        buf = self._ctypes.create_unicode_buffer(1024)
        if not self._k32.GetVolumePathNameW(self._ctypes.c_wchar_p(path), buf, len(buf)):
            raise OSError(self._ctypes.get_last_error(), f"GetVolumePathNameW({path}) failed")
        return buf.value

    def filesystem_name(self, root: str) -> str:
        fs = self._ctypes.create_unicode_buffer(261)
        ok = self._k32.GetVolumeInformationW(self._ctypes.c_wchar_p(root), None, 0, None, None, None, fs, len(fs))
        if not ok:
            raise OSError(self._ctypes.get_last_error(), f"GetVolumeInformationW({root}) failed")
        return fs.value

    def _volume_device(self, root: str) -> str:
        buf = self._ctypes.create_unicode_buffer(1024)
        if not self._k32.GetVolumeNameForVolumeMountPointW(self._ctypes.c_wchar_p(root), buf, len(buf)):
            raise OSError(self._ctypes.get_last_error(), f"GetVolumeNameForVolumeMountPointW({root}) failed")
        return buf.value.rstrip("\\")

    def _ioctl(self, root: str, code: int, inbuf: bytes, outsize: int) -> bytes:
        ct, wt, k32 = self._ctypes, self._wt, self._k32
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
        k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, wt.LPVOID, wt.DWORD,
                                        ct.POINTER(wt.DWORD), wt.LPVOID]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        handle = k32.CreateFileW(self._volume_device(root), 0, 0x1 | 0x2, None, 3, 0, None)
        if handle in (None, wt.HANDLE(-1).value):
            raise OSError(ct.get_last_error(), f"cannot open the volume of {root}")
        try:
            while True:
                out = ct.create_string_buffer(outsize)
                returned = wt.DWORD(0)
                inb = ct.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
                ok = k32.DeviceIoControl(handle, code, inb, len(inbuf), out, outsize, ct.byref(returned), None)
                if ok:
                    return out.raw[:returned.value]
                err = ct.get_last_error()
                if err in (122, 234) and outsize < 1 << 16:   # ERROR_INSUFFICIENT_BUFFER, ERROR_MORE_DATA
                    outsize *= 4
                    continue
                raise OSError(err, f"DeviceIoControl({code:#x}) on {root} failed")
        finally:
            k32.CloseHandle(handle)

    def bus_type(self, root: str) -> tuple[int, bool]:
        """(STORAGE_BUS_TYPE, RemovableMedia) from IOCTL_STORAGE_QUERY_PROPERTY / StorageDeviceProperty."""
        import struct
        raw = self._ioctl(root, 0x2D1400, struct.pack("<II4x", 0, 0), 1024)
        if len(raw) < 32:
            raise OSError(0, "STORAGE_DEVICE_DESCRIPTOR too short")
        removable = bool(raw[10])
        bus = struct.unpack_from("<I", raw, 28)[0]
        return bus, removable

    def physical_disks(self, root: str) -> tuple[int, ...]:
        """The physical disk numbers under the volume (IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS)."""
        import struct
        raw = self._ioctl(root, 0x560000, b"", 8 + 24 * 4)
        count = struct.unpack_from("<I", raw, 0)[0]
        return tuple(sorted({struct.unpack_from("<I", raw, 8 + 24 * i)[0] for i in range(count)}))

    def cloud_roots(self) -> list[str]:
        roots = [os.environ.get(n) for n in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            info = os.path.join(base, "Dropbox", "info.json")
            try:
                import json
                with open(info, encoding="utf-8") as handle:
                    data = json.load(handle)
                roots += [v.get("path") for v in data.values() if isinstance(v, dict)]
            except (OSError, ValueError, AttributeError):
                pass
        return [r for r in roots if r]

    def attributes(self, path: str) -> tuple[int, int]:
        """(file attributes, reparse tag) without following a final reparse point."""
        st = os.lstat(path)
        return getattr(st, "st_file_attributes", 0), getattr(st, "st_reparse_tag", 0)

    def computer_names(self) -> set[str]:
        names = {"localhost", "127.0.0.1", "::1", "[::1]", "0:0:0:0:0:0:0:1"}
        for name in (os.environ.get("COMPUTERNAME"), socket.gethostname()):
            if name:
                names |= {name.lower(), name.split(".")[0].lower()}
        return names


class PosixStorageAdapter:
    """POSIX has no portable removable/fixed query: only a known-network filesystem is classified (refused); a
    known local filesystem is FIXED_LOCAL with unknown physical disks; anything else UNKNOWN."""
    LOCAL_FS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "apfs", "hfs", "f2fs", "jfs", "reiserfs"})

    def filesystem_of(self, path: str) -> str | None:
        from backend.instance_lock import _posix_filesystem
        return _posix_filesystem(path)


def default_adapter():
    return WindowsStorageAdapter() if os.name == "nt" else PosixStorageAdapter()


# ---- the classifier ----------------------------------------------------------------------------------------------

def _strip_long_prefix(text: str) -> str:
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("\\\\?\\") and len(text) > 6 and text[5] == ":":
        return text[4:]
    return text


def _unc_host(text: str) -> str:
    return text.lstrip("\\/").replace("/", "\\").split("\\", 1)[0].lower()


def _is_unc(text: str) -> bool:
    return (text.startswith("\\\\") or text.startswith("//")) and not text.startswith(("\\\\?\\", "\\\\.\\"))


def _under(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root)).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def classify_path(path, adapter=None) -> StorageVerdict:
    """The storage category of `path` (a file that may not exist yet, or a directory). Never raises."""
    try:
        text = os.fspath(path)
    except TypeError:
        return StorageVerdict(INVALID_PATH, "not a path", repr(path))
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        return StorageVerdict(INVALID_PATH, "an empty path or a path containing NUL", repr(text))
    if os.name != "nt" and adapter is None:
        return _classify_posix(text, PosixStorageAdapter())
    if isinstance(adapter, PosixStorageAdapter):
        return _classify_posix(text, adapter)
    try:
        adapter = adapter or WindowsStorageAdapter()
    except Exception as e:  # noqa: BLE001 — no Windows API: cannot establish
        return StorageVerdict(UNKNOWN, f"the Windows storage API is unavailable ({type(e).__name__}: {e})", text)
    try:
        return _classify_windows(text, text, adapter, 0, {})
    except Exception as e:  # noqa: BLE001 — fail closed on anything unforeseen
        return StorageVerdict(UNKNOWN, f"classification failed ({type(e).__name__}: {e})", text)


def _classify_posix(text: str, adapter: PosixStorageAdapter) -> StorageVerdict:
    resolved = os.path.realpath(text)
    fstype = adapter.filesystem_of(resolved)
    from backend.instance_lock import _NETWORK_FILESYSTEMS
    if fstype in _NETWORK_FILESYSTEMS:
        return StorageVerdict(NETWORK_UNC, f"a network filesystem ({fstype})", text, resolved, filesystem=fstype)
    if fstype in PosixStorageAdapter.LOCAL_FS:
        return StorageVerdict(FIXED_LOCAL, f"a local filesystem ({fstype})", text, resolved, filesystem=fstype)
    return StorageVerdict(UNKNOWN, f"filesystem {fstype!r} is not known to be local", text, resolved,
                          filesystem=fstype)


def _unc_verdict(original: str, unc: str, adapter, details: dict, how: str) -> StorageVerdict:
    host = _unc_host(unc)
    try:
        loopback = host in adapter.computer_names() or host.startswith("127.")
    except Exception:  # noqa: BLE001
        loopback = False
    return StorageVerdict(NETWORK_UNC, f"{how} a network (UNC) path on host {host!r}"
                          + (" (this machine, over the network redirector)" if loopback else ""),
                          original, unc, loopback=loopback, details=details)


def _classify_windows(original: str, text: str, adapter, depth: int, details: dict) -> StorageVerdict:
    if depth > _MAX_DEPTH:
        return StorageVerdict(UNKNOWN, "too many levels of subst / links", original, details=details)
    text = _strip_long_prefix(text.replace("/", "\\") if text.startswith("//") else text)
    if _is_unc(text):
        return _unc_verdict(original, text, adapter, details, "it is" if depth == 0 else "it resolves to")
    if text.startswith(("\\\\.\\", "\\\\?\\")):
        return StorageVerdict(INVALID_PATH, "a device-namespace or volume-GUID path, not a drive path", original,
                              details=details)
    absolute = os.path.abspath(text)
    drive, rest = os.path.splitdrive(absolute)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        return StorageVerdict(INVALID_PATH, f"no drive letter in {absolute}", original, details=details)
    if any(ch in _RESERVED_CHARS or ord(ch) < 32 for ch in rest) or ":" in rest:
        return StorageVerdict(INVALID_PATH, "the path contains a reserved character", original, details=details)
    root = drive.upper() + "\\"

    kind = adapter.drive_type(root)
    if kind == DRIVE_NO_ROOT_DIR:
        return StorageVerdict(UNAVAILABLE, f"drive {drive.upper()} does not exist", original, absolute,
                              drive_type=DRIVE_TYPE_NAMES[kind], details=details)
    if kind == DRIVE_REMOTE:
        return StorageVerdict(NETWORK_MAPPED, f"drive {drive.upper()} is a network drive", original, absolute,
                              drive_type=DRIVE_TYPE_NAMES[kind], details=details)
    mapped = adapter.mapped_connection(drive.upper())
    if mapped is not None:
        host = _unc_host(mapped) if mapped else ""
        try:
            loopback = bool(host) and (host in adapter.computer_names() or host.startswith("127."))
        except Exception:  # noqa: BLE001
            loopback = False
        return StorageVerdict(NETWORK_MAPPED, f"drive {drive.upper()} is mapped to a network share"
                              + (f" ({mapped})" if mapped else " (currently unavailable)"), original, absolute,
                              drive_type=DRIVE_TYPE_NAMES.get(kind, str(kind)), loopback=loopback,
                              details=dict(details, remote=mapped or None))
    device = adapter.query_dos_device(drive.upper())
    if device and device.startswith("\\??\\"):
        target = device[4:]
        underlying = (target[:-1] if target.endswith("\\") else target) + rest
        details = dict(details, subst_of=f"{drive.upper()} -> {target}")
        if target.upper().startswith("UNC\\"):
            return _unc_verdict(original, "\\\\" + target[4:] + rest, adapter, details, "it is a subst drive onto")
        return _classify_windows(original, underlying, adapter, depth + 1, details)
    if device and "LanmanRedirector" in device or (device and "Mup" in device):
        return StorageVerdict(NETWORK_MAPPED, f"drive {drive.upper()} is a network redirector device ({device})",
                              original, absolute, details=details)

    resolved = adapter.realpath(absolute)
    resolved = _strip_long_prefix(resolved)
    if _is_unc(resolved):
        return _unc_verdict(original, resolved, adapter, dict(details, resolved_from=absolute),
                            "through a link or junction it resolves to")
    if os.path.normcase(os.path.splitdrive(resolved)[0]) != os.path.normcase(drive):
        # A junction or symbolic link onto another drive: classify where the data really is.
        return _classify_windows(original, resolved, adapter, depth + 1, dict(details, resolved_from=absolute))
    if os.path.normcase(resolved) != os.path.normcase(absolute):
        details = dict(details, resolved_from=absolute)

    volume = adapter.volume_path_name(resolved)
    vkind = adapter.drive_type(volume) if os.path.normcase(volume) != os.path.normcase(root) else kind
    base = dict(path=original, canonical_path=resolved, volume_root=volume,
                drive_type=DRIVE_TYPE_NAMES.get(vkind, str(vkind)), details=details)
    if vkind == DRIVE_NO_ROOT_DIR:
        return StorageVerdict(UNAVAILABLE, f"the volume {volume} is not available", **base)
    if vkind == DRIVE_REMOTE:
        return StorageVerdict(NETWORK_MAPPED, f"the volume {volume} is a network volume", **base)
    if vkind == DRIVE_REMOVABLE:
        return StorageVerdict(REMOVABLE, f"the volume {volume} is a removable drive", **base)
    if vkind in (DRIVE_CDROM, DRIVE_RAMDISK):
        return StorageVerdict(OPTICAL_OR_RAM, f"the volume {volume} is a {DRIVE_TYPE_NAMES[vkind]}", **base)
    if vkind != DRIVE_FIXED:
        return StorageVerdict(UNKNOWN, f"the volume {volume} has drive type {vkind} (unknown)", **base)

    fs = adapter.filesystem_name(volume)
    base["filesystem"] = fs
    if (fs or "").upper() in CLOUD_FILESYSTEMS:
        return StorageVerdict(CLOUD_SYNCED, f"the volume {volume} is a cloud virtual drive ({fs})", **base)
    cloud = _cloud_marker(resolved, adapter)
    if cloud:
        return StorageVerdict(CLOUD_SYNCED, cloud, **base)
    bus, removable_media = adapter.bus_type(volume)
    base["bus_type"] = BUS_TYPE_NAMES.get(bus, str(bus))
    disks = tuple(adapter.physical_disks(volume))
    base["physical_disks"] = disks
    if removable_media or bus in REMOVABLE_BUSES:
        return StorageVerdict(REMOVABLE, f"the volume {volume} is on a {BUS_TYPE_NAMES.get(bus, bus)} disk"
                              + (" with removable media" if removable_media else "")
                              + " (Windows reports it as fixed)", **base)
    if bus in NETWORK_BUSES:
        return StorageVerdict(NETWORK_BLOCK, f"the volume {volume} is network block storage "
                              f"({BUS_TYPE_NAMES.get(bus, bus)})", **base)
    if bus in VIRTUAL_BUSES:
        return StorageVerdict(VIRTUAL_DISK, f"the volume {volume} is a virtual disk ({BUS_TYPE_NAMES.get(bus, bus)})",
                              **base)
    if bus not in LOCAL_BUSES:
        return StorageVerdict(UNKNOWN, f"the volume {volume} has an unknown bus type ({bus})", **base)
    if (fs or "").upper() not in ACCEPTED_FILESYSTEMS:
        return StorageVerdict(UNKNOWN, f"the volume {volume} has filesystem {fs!r}, not NTFS or ReFS", **base)
    if not disks:
        return StorageVerdict(UNKNOWN, f"the physical disk of the volume {volume} could not be established", **base)
    return StorageVerdict(FIXED_LOCAL, f"an internal fixed {fs} volume ({volume}, {BUS_TYPE_NAMES.get(bus, bus)} "
                          f"disk {', '.join(map(str, disks))})", **base)


def _cloud_marker(resolved: str, adapter) -> str | None:
    for root in adapter.cloud_roots():
        if _under(resolved, root):
            return f"the path is inside the cloud-synced folder {root}"
    if "onedrive" in resolved.replace("\\", "/").lower().split("/"):
        return "the path contains a OneDrive folder"
    current = resolved
    while True:
        if os.path.lexists(current):
            attributes, tag = adapter.attributes(current)
            if attributes & CLOUD_ATTRIBUTES:
                return f"{current} carries cloud-placeholder attributes ({attributes:#x})"
            if tag and (tag & IO_REPARSE_TAG_CLOUD_MASK) == IO_REPARSE_TAG_CLOUD:
                return f"{current} is a cloud-files reparse point ({tag:#x})"
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def same_physical_disk(a: StorageVerdict, b: StorageVerdict) -> bool | None:
    """True when both verdicts share a physical disk, False when they provably do not, None when unknown."""
    if not a.physical_disks or not b.physical_disks:
        return None
    return bool(set(a.physical_disks) & set(b.physical_disks))


def describe(verdict: StorageVerdict) -> str:
    return f"{verdict.category} ({verdict.reason})"
# Operators run the classification with `python -m backend.db_maintenance storage-check [--db PATH] [--backup FILE]`.
