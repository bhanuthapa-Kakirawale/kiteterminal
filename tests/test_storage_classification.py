"""Storage classification: the fixed-local-drive requirement (V12 final readiness, Phase B).

Two layers:
  * FakeAdapter tests: every category and edge case, with the OS answers injected (no dependence on this machine);
  * Windows integration tests: the REAL adapter on this machine's temp directory, a real junction, an unused drive
    letter and a UNC string (classified from its text: no share is contacted).
"""

import os
import string
import subprocess
import sys
import time

import pytest

from backend import storage_classification as sc

WINDOWS = os.name == "nt"


class FakeAdapter:
    """The OS answers, per drive letter. Any method listed in `fail` raises (an API failure)."""

    def __init__(self, drives=None, links=None, volumes=None, cloud_roots=(), attributes=None, fail=(),
                 names=("mypc",)):
        self.drives = drives if drives is not None else {"C": fixed()}
        self.links = links or {}
        self.volumes = volumes or {}
        self._cloud = list(cloud_roots)
        self._attributes = attributes or {}
        self.fail = set(fail)
        self.names = set(names) | {"localhost", "127.0.0.1", "::1"}
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail or "*" in self.fail:
            raise OSError(5, f"simulated {name} failure")

    def _drive(self, root):
        return self.drives.get(root[0].upper(), {"type": sc.DRIVE_NO_ROOT_DIR})

    def drive_type(self, root):
        self._call("drive_type")
        for mount, info in self.volumes.items():
            if os.path.normcase(root) == os.path.normcase(mount):
                return info["type"]
        return self._drive(root)["type"]

    def mapped_connection(self, drive):
        self._call("mapped_connection")
        return self._drive(drive).get("mapped")

    def query_dos_device(self, drive):
        self._call("query_dos_device")
        return self._drive(drive).get("dos", r"\Device\HarddiskVolume3")

    def realpath(self, path):
        self._call("realpath")
        for link, target in self.links.items():
            if os.path.normcase(path).startswith(os.path.normcase(link)):
                return target + path[len(link):]
        return os.path.normpath(path)

    def volume_path_name(self, path):
        self._call("volume_path_name")
        for mount in sorted(self.volumes, key=len, reverse=True):
            if os.path.normcase(path).startswith(os.path.normcase(mount)):
                return mount
        return path[:3]

    def _info(self, root):
        for mount, info in self.volumes.items():
            if os.path.normcase(root) == os.path.normcase(mount):
                return info
        return self._drive(root)

    def filesystem_name(self, root):
        self._call("filesystem_name")
        return self._info(root).get("fs", "NTFS")

    def bus_type(self, root):
        self._call("bus_type")
        info = self._info(root)
        return info.get("bus", 17), info.get("removable", False)

    def physical_disks(self, root):
        self._call("physical_disks")
        return self._info(root).get("disks", (0,))

    def cloud_roots(self):
        self._call("cloud_roots")
        return self._cloud

    def attributes(self, path):
        self._call("attributes")
        return self._attributes.get(os.path.normcase(path), (0, 0))

    def computer_names(self):
        return self.names


def fixed(**kw):
    return dict({"type": sc.DRIVE_FIXED, "fs": "NTFS", "bus": 17, "disks": (0,)}, **kw)


def classify(path, **kw):
    return sc.classify_path(path, adapter=FakeAdapter(**kw))


# ---------------------------------------------------------------- fixed local: the one acceptable category

def test_s01_an_internal_fixed_ntfs_nvme_volume_is_fixed_local_and_acceptable():
    v = classify(r"C:\kite\backend\kite_terminal.db")
    assert v.category == sc.FIXED_LOCAL and v.acceptable_for_database and v.refusal() is None
    assert v.physical_disks == (0,) and v.filesystem == "NTFS" and v.bus_type == "NVMe"


@pytest.mark.parametrize("bus", [1, 3, 8, 10, 11, 16, 17])
def test_s02_every_internal_bus_on_a_fixed_ntfs_volume_is_accepted(bus):
    assert classify(r"D:\db.db", drives={"D": fixed(bus=bus)}).category == sc.FIXED_LOCAL


def test_s03_refs_is_accepted_like_ntfs():
    assert classify(r"D:\db.db", drives={"D": fixed(fs="ReFS")}).category == sc.FIXED_LOCAL


# ---------------------------------------------------------------- removable

def test_s04_a_removable_drive_type_is_refused():
    v = classify(r"E:\db.db", drives={"E": fixed(type=sc.DRIVE_REMOVABLE)})
    assert v.category == sc.REMOVABLE and not v.acceptable_for_database
    assert "STORAGE_NOT_FIXED_LOCAL" in v.refusal() and "internal fixed disk" in v.refusal()


@pytest.mark.parametrize("bus", [4, 7, 12, 13])
def test_s05_a_usb_firewire_sd_or_mmc_disk_reported_as_fixed_is_still_removable(bus):
    v = classify(r"E:\db.db", drives={"E": fixed(bus=bus)})
    assert v.category == sc.REMOVABLE and "reports it as fixed" in v.reason


def test_s06_removable_media_on_a_fixed_bus_is_removable():
    assert classify(r"E:\db.db", drives={"E": fixed(removable=True)}).category == sc.REMOVABLE


def test_s07_a_mounted_folder_onto_a_removable_volume_is_classified_by_that_volume():
    v = classify(r"C:\mnt\usb\db.db", volumes={"C:\\mnt\\usb\\": fixed(type=sc.DRIVE_REMOVABLE)})
    assert v.category == sc.REMOVABLE and v.volume_root == "C:\\mnt\\usb\\"


# ---------------------------------------------------------------- network

@pytest.mark.parametrize("path", [r"\\server\share\kite_terminal.db", r"\\?\UNC\server\share\kite_terminal.db",
                                  "//server/share/kite_terminal.db"])
def test_s08_a_unc_path_is_network_and_classified_without_any_os_query(path):
    adapter = FakeAdapter(fail={"*"})
    v = sc.classify_path(path, adapter=adapter)
    assert v.category == sc.NETWORK_UNC and not v.acceptable_for_database
    assert adapter.calls == []          # the share is never contacted


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "mypc", "MYPC"])
def test_s09_a_unc_path_back_to_this_machine_is_network_and_marked_loopback(host):
    v = classify(rf"\\{host}\c$\kite\db.db")
    assert v.category == sc.NETWORK_UNC and v.loopback


def test_s10_a_drive_letter_of_type_remote_is_a_mapped_network_drive():
    v = classify(r"Z:\db.db", drives={"Z": {"type": sc.DRIVE_REMOTE}})
    assert v.category == sc.NETWORK_MAPPED


@pytest.mark.parametrize("mapped", [r"\\nas\trading", ""])
def test_s11_a_drive_letter_mapped_to_a_share_is_refused_even_if_it_reports_fixed(mapped):
    v = classify(r"Z:\db.db", drives={"Z": fixed(mapped=mapped)})
    assert v.category == sc.NETWORK_MAPPED and "mapped" in v.reason


@pytest.mark.parametrize("remote, loopback", [(r"\\nas\trading", False), (r"\\localhost\c$", True),
                                              (r"\\MYPC\share", True), (r"\\127.0.0.1\x", True)])
def test_s11b_a_mapped_drive_records_its_share_and_whether_it_points_back_to_this_machine(remote, loopback):
    v = classify(r"Z:\db.db", drives={"Z": fixed(mapped=remote)})
    assert v.category == sc.NETWORK_MAPPED and v.details["remote"] == remote and v.loopback is loopback


def test_s12_a_redirector_device_behind_a_letter_is_network():
    v = classify(r"Z:\db.db", drives={"Z": fixed(dos=r"\Device\LanmanRedirector\;Z:0000\nas\share")})
    assert v.category == sc.NETWORK_MAPPED


def test_s13_iscsi_block_storage_is_refused():
    assert classify(r"F:\db.db", drives={"F": fixed(bus=9)}).category == sc.NETWORK_BLOCK


# ---------------------------------------------------------------- subst, junctions, symlinks, traversal

def test_s14_a_subst_drive_onto_a_unc_share_is_network():
    v = classify(r"S:\db.db", drives={"S": fixed(dos=r"\??\UNC\nas\share\folder")})
    assert v.category == sc.NETWORK_UNC and "subst" in v.reason


def test_s15_a_subst_drive_onto_a_local_fixed_folder_is_classified_by_the_real_drive():
    v = classify(r"S:\db.db", drives={"S": fixed(dos="\\??\\C:\\work\\"), "C": fixed()})
    assert v.category == sc.FIXED_LOCAL and v.details["subst_of"].startswith("S:")
    assert os.path.normcase(v.canonical_path) == os.path.normcase(r"C:\work\db.db")


def test_s16_a_subst_drive_onto_a_usb_disk_is_removable():
    v = classify(r"S:\db.db", drives={"S": fixed(dos="\\??\\E:\\data\\"), "E": fixed(bus=7)})
    assert v.category == sc.REMOVABLE


def test_s17_a_junction_or_symlink_onto_a_share_is_network():
    v = classify(r"C:\kite\backend\db.db", links={r"C:\kite\backend": r"\\nas\share\backend"})
    assert v.category == sc.NETWORK_UNC and v.details.get("resolved_from")


def test_s18_a_junction_onto_another_drive_is_classified_where_the_data_really_is():
    v = classify(r"C:\kite\backend\db.db", links={r"C:\kite\backend": r"E:\backend"},
                 drives={"C": fixed(), "E": fixed(bus=7)})
    assert v.category == sc.REMOVABLE


def test_s19_dot_dot_segments_are_canonicalised_before_classification():
    v = classify(r"C:\kite\..\..\..\other\db.db")
    assert v.category == sc.FIXED_LOCAL
    assert os.path.normcase(v.canonical_path) == os.path.normcase(r"C:\other\db.db")


def test_s20_dot_dot_through_a_junction_cannot_hide_a_share():
    v = classify(r"C:\safe\..\kite\db.db", links={r"C:\kite": r"\\nas\share"})
    assert v.category == sc.NETWORK_UNC


def test_s21_a_link_loop_is_unknown_not_accepted():
    v = classify(r"C:\a\db.db", links={r"C:\a": r"D:\b", r"D:\b": r"C:\a"}, drives={"C": fixed(), "D": fixed()})
    assert v.category == sc.UNKNOWN and not v.acceptable_for_database


# ---------------------------------------------------------------- unavailable, invalid, unknown

def test_s22_a_drive_that_does_not_exist_is_unavailable():
    assert classify(r"Q:\db.db", drives={}).category == sc.UNAVAILABLE


@pytest.mark.parametrize("path", ["", "   ", "C:\\db\x00.db", r"C:\db|x.db", r"C:\db<x>.db", r"C:\a\b:c.db",
                                  r"\\.\PhysicalDrive0", r"\\?\Volume{0000}\db.db", None, 42])
def test_s23_invalid_paths_are_refused(path):
    v = sc.classify_path(path, adapter=FakeAdapter())
    assert v.category == sc.INVALID_PATH and not v.acceptable_for_database


@pytest.mark.parametrize("method", ["drive_type", "mapped_connection", "query_dos_device", "realpath",
                                    "volume_path_name", "filesystem_name", "bus_type", "physical_disks",
                                    "cloud_roots", "attributes"])
def test_s24_any_os_query_failure_is_unknown_and_refused(method):
    v = classify(r"C:\kite\db.db", fail={method})
    assert v.category == sc.UNKNOWN and not v.acceptable_for_database and "STORAGE_NOT_FIXED_LOCAL" in v.refusal()


@pytest.mark.parametrize("drive", [fixed(type=sc.DRIVE_UNKNOWN), fixed(type=99), fixed(bus=0), fixed(bus=55),
                                   fixed(fs="FAT32"), fixed(fs="exFAT"), fixed(fs=""), fixed(disks=())])
def test_s25_unknown_or_unexpected_answers_are_unknown_and_refused(drive):
    v = classify(r"C:\kite\db.db", drives={"C": drive})
    assert v.category == sc.UNKNOWN and not v.acceptable_for_database


def test_s26_a_failing_adapter_constructor_is_unknown():
    class Broken:
        def __init__(self):
            raise OSError("no kernel32")
    original = sc.WindowsStorageAdapter
    sc.WindowsStorageAdapter = Broken
    try:
        if WINDOWS:
            assert sc.classify_path(r"C:\db.db").category == sc.UNKNOWN
    finally:
        sc.WindowsStorageAdapter = original


@pytest.mark.parametrize("drive, category", [(fixed(type=sc.DRIVE_CDROM), sc.OPTICAL_OR_RAM),
                                             (fixed(type=sc.DRIVE_RAMDISK), sc.OPTICAL_OR_RAM),
                                             (fixed(bus=14), sc.VIRTUAL_DISK), (fixed(bus=15), sc.VIRTUAL_DISK)])
def test_s27_optical_ram_and_virtual_disks_are_refused(drive, category):
    assert classify(r"V:\db.db", drives={"V": drive}).category == category


# ---------------------------------------------------------------- cloud-synced folders

def test_s28_a_path_inside_a_onedrive_root_is_cloud_synced():
    v = classify(r"C:\Users\op\OneDrive\kite\db.db", cloud_roots=[r"C:\Users\op\OneDrive"])
    assert v.category == sc.CLOUD_SYNCED and not v.acceptable_for_database


def test_s29_a_path_inside_a_dropbox_root_is_cloud_synced():
    assert classify(r"D:\Dropbox\kite\db.db", cloud_roots=[r"D:\Dropbox"],
                    drives={"D": fixed()}).category == sc.CLOUD_SYNCED


def test_s30_a_cloud_virtual_drive_filesystem_is_cloud_synced():
    assert classify(r"G:\db.db", drives={"G": fixed(fs="DriveFS")}).category == sc.CLOUD_SYNCED


@pytest.mark.parametrize("attributes", [(sc.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 0), (sc.FILE_ATTRIBUTE_PINNED, 0),
                                        (sc.FILE_ATTRIBUTE_OFFLINE, 0), (0, 0x9000601A)])
def test_s31_cloud_placeholder_attributes_or_reparse_tags_on_a_path_component_are_cloud_synced(tmp_path, attributes):
    folder = tmp_path / "synced"
    folder.mkdir()
    v = classify(str(folder / "db.db"), attributes={os.path.normcase(str(folder)): attributes},
                 drives={str(tmp_path)[0].upper(): fixed()})
    assert v.category == sc.CLOUD_SYNCED


def test_s32_a_folder_merely_named_like_a_cloud_service_elsewhere_is_not_cloud():
    assert classify(r"C:\data\Dropboxed\db.db", cloud_roots=[r"D:\Dropbox"]).category == sc.FIXED_LOCAL


# ---------------------------------------------------------------- the path changing between two checks

def test_s33_the_same_path_is_reclassified_on_every_call_never_cached():
    adapter = FakeAdapter()
    assert sc.classify_path(r"C:\db.db", adapter=adapter).category == sc.FIXED_LOCAL
    adapter.drives["C"] = {"type": sc.DRIVE_REMOTE}          # e.g. the letter was re-mapped to a share
    assert sc.classify_path(r"C:\db.db", adapter=adapter).category == sc.NETWORK_MAPPED


def test_s34_same_physical_disk_comparison():
    a = classify(r"D:\db.db", drives={"D": fixed(disks=(0,))})
    b = classify(r"E:\backup\x", drives={"E": fixed(disks=(0,))})
    c = classify(r"F:\backup\x", drives={"F": fixed(bus=7, disks=(1,))})
    unknown = classify(r"\\nas\share\x")
    assert sc.same_physical_disk(a, b) is True
    assert sc.same_physical_disk(a, c) is False
    assert sc.same_physical_disk(a, unknown) is None


def test_s35_the_record_holds_storage_facts_only():
    record = classify(r"C:\kite\db.db").as_record()
    assert set(record) == {"category", "acceptable_for_database", "reason", "canonical_path", "volume_root",
                           "drive_type", "filesystem", "bus_type", "physical_disks", "loopback", "details"}


# ---------------------------------------------------------------- Windows integration (the real adapter)

windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows storage APIs")


@windows_only
def test_w01_this_machines_temp_directory_is_classified_by_the_real_adapter(tmp_path):
    v = sc.classify_path(tmp_path / "x.db")
    assert v.category in {sc.FIXED_LOCAL, sc.REMOVABLE, sc.VIRTUAL_DISK, sc.UNKNOWN, sc.CLOUD_SYNCED}
    if v.category == sc.FIXED_LOCAL:
        assert v.physical_disks and v.filesystem.upper() in sc.ACCEPTED_FILESYSTEMS and v.volume_root
    else:  # a host whose temp dir is not an internal fixed disk: refused, never accepted
        assert v.refusal() and not v.acceptable_for_database


@windows_only
def test_w02_a_real_junction_is_resolved_to_its_target(tmp_path):
    target = tmp_path / "real_folder"
    target.mkdir()
    link = tmp_path / "junction"
    out = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    direct = sc.classify_path(target / "x.db")
    via = sc.classify_path(link / "x.db")
    assert via.category == direct.category
    assert os.path.normcase(via.canonical_path) == os.path.normcase(str(target / "x.db"))
    assert via.details.get("resolved_from")


@windows_only
def test_w03_an_unused_drive_letter_is_unavailable():
    adapter = sc.WindowsStorageAdapter()
    free = [d for d in reversed(string.ascii_uppercase[3:]) if adapter.drive_type(f"{d}:\\") == sc.DRIVE_NO_ROOT_DIR]
    assert free, "no unused drive letter on this machine"
    assert sc.classify_path(f"{free[0]}:\\kite\\db.db").category == sc.UNAVAILABLE


@windows_only
def test_w04_a_unc_string_is_network_immediately_without_contacting_a_host():
    started = time.monotonic()
    v = sc.classify_path(r"\\203.0.113.7\share\kite_terminal.db")   # TEST-NET-3: never routable
    assert v.category == sc.NETWORK_UNC and time.monotonic() - started < 1.0


@windows_only
def test_w05_the_real_adapter_answers_every_query_for_the_system_drive():
    adapter = sc.WindowsStorageAdapter()
    root = os.environ.get("SystemDrive", "C:") + "\\"
    assert adapter.drive_type(root) == sc.DRIVE_FIXED
    assert adapter.filesystem_name(root)
    bus, _removable = adapter.bus_type(root)
    assert bus in sc.BUS_TYPE_NAMES
    assert adapter.physical_disks(root)
    assert adapter.mapped_connection(root[:2]) is None
