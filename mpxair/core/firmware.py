"""ArduPilot firmware: manifest download, .apj files and the ChibiOS/PX4 bootloader protocol.

Protocol as used by Mission Planner (ExtLibs/px4uploader) and ArduPilot's uploader.py.
"""
import base64
import gzip
import json
import os
import time
import urllib.request
import zlib

from . import config
from .transport import SerialTransport, TransportError, list_serial_ports

MANIFEST_URL = "https://firmware.ardupilot.org/manifest.json.gz"
USER_AGENT = "MPXAir/1.0"

INSYNC, OK, FAILED, INVALID = 0x12, 0x10, 0x11, 0x13
EOC = 0x20
GET_SYNC, GET_DEVICE, CHIP_ERASE, PROG_MULTI, GET_CRC, REBOOT = 0x21, 0x22, 0x23, 0x27, 0x29, 0x30
INFO_BL_REV, INFO_BOARD_ID, INFO_BOARD_REV, INFO_FLASH_SIZE = 1, 2, 3, 4
PROG_MULTI_MAX = 252

VEHICLE_TYPES = [("Copter", "Copter"), ("Plane", "Plane"), ("Rover", "Rover"), ("Sub", "Sub"),
                 ("Helicopter", "Heli"), ("Antenna tracker", "AntennaTracker"), ("Blimp", "Blimp")]
RELEASES = [("Stable", "OFFICIAL"), ("Beta", "BETA"), ("Latest (development)", "DEV")]


class FirmwareError(Exception):
    pass


# ------------------------------------------------------------------- manifest
def manifest_cache_path():
    return os.path.join(config.cache_dir(), "manifest.json")


def load_manifest(refresh=False, max_age_h=24):
    """Return the parsed manifest, downloading it when missing or stale (if online)."""
    path = manifest_cache_path()
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600
    if refresh or not fresh:
        try:
            req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            data = gzip.decompress(raw)
            json.loads(data)          # validate before replacing the cache
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
        except Exception as e:
            if not os.path.exists(path):
                raise FirmwareError(f"Could not download the firmware list: {e}")
    with open(path) as fh:
        return json.load(fh)


def find_firmware(manifest, board_id, vehicle, release):
    """Matching .apj entries, best first."""
    out = []
    seen = set()
    for fw in manifest.get("firmware", []):
        if fw.get("url") in seen:
            continue
        if fw.get("format") != "apj":
            continue
        if fw.get("vehicletype") != vehicle:
            continue
        if fw.get("mav-firmware-version-type") != release:
            continue
        try:
            if int(fw.get("board_id", -1)) != int(board_id):
                continue
        except (TypeError, ValueError):
            continue
        seen.add(fw.get("url"))
        out.append(fw)
    # prefer the plain build over "-bdshot"/"-heli" variants and the latest version
    out.sort(key=lambda f: (f.get("platform", "").count("-"), [-int(x) for x in
                            str(f.get("mav-firmware-version", "0.0.0")).split(".") if x.isdigit()]))
    return out


def boards_in_manifest(manifest):
    names = {}
    for fw in manifest.get("firmware", []):
        if fw.get("format") == "apj":
            try:
                names.setdefault(int(fw["board_id"]), set()).add(fw.get("platform", "?"))
            except (KeyError, ValueError, TypeError):
                pass
    return names


def download(url, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        chunks = []
        got = 0
        while True:
            b = r.read(65536)
            if not b:
                break
            chunks.append(b)
            got += len(b)
            if progress:
                progress(got, total)
    return b"".join(chunks)


# ------------------------------------------------------------------- apj file
class Firmware:
    def __init__(self, data):
        try:
            desc = json.loads(data)
        except ValueError:
            raise FirmwareError("This is not an ArduPilot .apj firmware file.")
        if "image" not in desc or "board_id" not in desc:
            raise FirmwareError("The firmware file is missing its image or board id.")
        self.desc = desc
        self.board_id = int(desc["board_id"])
        self.image = bytearray(zlib.decompress(base64.b64decode(desc["image"])))
        while len(self.image) % 4:
            self.image.append(0xFF)
        self.extf = None
        if desc.get("extf_image"):
            self.extf = bytearray(zlib.decompress(base64.b64decode(desc["extf_image"])))
        self.summary = desc.get("summary", "")
        self.version = desc.get("version", "")
        self.git = desc.get("git_identity", "")

    @classmethod
    def from_file(cls, path):
        with open(path, "rb") as fh:
            return cls(fh.read())


def crc32_px4(data, state=0):
    """CRC used by the bootloader: reflected 0xEDB88320, initial 0, no final xor."""
    return zlib.crc32(bytes(data), state ^ 0xFFFFFFFF) ^ 0xFFFFFFFF


def image_crc(image, flash_size):
    state = crc32_px4(image, 0)
    pad = b"\xff\xff\xff\xff"
    # the bootloader CRCs the whole flash area, unwritten words read as 0xFF
    remaining = (flash_size - len(image)) // 4
    if remaining > 0:
        state = crc32_px4(pad * remaining, state)
    return state


# ----------------------------------------------------------------- bootloader
class Bootloader:
    def __init__(self, device, baud=115200, log=None):
        self.device = device
        self.port = SerialTransport(device, baud)
        self.log = log or (lambda s: None)
        self.buf = b""
        self.bl_rev = self.board_id = self.board_rev = self.flash_size = 0

    def close(self):
        self.port.close()

    def _recv(self, n, timeout=1.0):
        end = time.monotonic() + timeout
        while len(self.buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                raise FirmwareError("Timed out waiting for the bootloader")
            self.buf += self.port.read(min(0.2, left))
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, data):
        self.port.write(bytes(data))

    def _get_sync(self, timeout=1.0):
        c = self._recv(1, timeout)
        if c[0] != INSYNC:
            raise FirmwareError(f"Bootloader out of sync (got 0x{c[0]:02x})")
        c = self._recv(1, timeout)
        if c[0] == INVALID:
            raise FirmwareError("Bootloader reports an invalid operation")
        if c[0] == FAILED:
            raise FirmwareError("Bootloader reports the operation failed")
        if c[0] != OK:
            raise FirmwareError(f"Unexpected bootloader reply 0x{c[0]:02x}")

    def sync(self):
        self.buf = b""
        try:
            self.port.read(0.05)
        except TransportError:
            pass
        self._send([GET_SYNC, EOC])
        self._get_sync(0.5)

    def _info(self, param):
        self._send([GET_DEVICE, param, EOC])
        v = int.from_bytes(self._recv(4), "little")
        self._get_sync()
        return v

    def identify(self):
        self.sync()
        self.bl_rev = self._info(INFO_BL_REV)
        if not 2 <= self.bl_rev <= 20:
            raise FirmwareError(f"Unsupported bootloader version {self.bl_rev}")
        self.board_id = self._info(INFO_BOARD_ID)
        self.board_rev = self._info(INFO_BOARD_REV)
        self.flash_size = self._info(INFO_FLASH_SIZE)
        return self.board_id

    def erase(self):
        self.sync()
        self._info(INFO_BL_REV)          # works around an old bootloader bug, as MP does
        self.log("Erasing flash (this can take up to 20 seconds)…")
        self._send([CHIP_ERASE, EOC])
        self._get_sync(timeout=30)

    def program(self, image, progress=None):
        total = len(image)
        for ofs in range(0, total, PROG_MULTI_MAX):
            chunk = image[ofs:ofs + PROG_MULTI_MAX]
            self._send(bytes([PROG_MULTI, len(chunk)]) + bytes(chunk) + bytes([EOC]))
            self._get_sync(2.0)
            if progress:
                progress(ofs + len(chunk), total)

    def verify(self, image):
        expect = image_crc(image, self.flash_size)
        self._send([GET_CRC, EOC])
        got = int.from_bytes(self._recv(4, 5.0), "little")
        self._get_sync()
        if got != expect:
            raise FirmwareError(f"Verification failed (CRC 0x{got:08x}, expected 0x{expect:08x})")

    def reboot(self):
        try:
            self._send([REBOOT, EOC])
        except TransportError:
            pass


def find_bootloader(timeout=30.0, log=None, cancel=None, exclude=()):
    """Poll serial ports until one answers as a bootloader. Returns an identified Bootloader."""
    log = log or (lambda s: None)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cancel is not None and cancel.is_set():
            raise FirmwareError("Cancelled")
        for dev, _desc in list_serial_ports():
            if dev in exclude:
                continue
            try:
                bl = Bootloader(dev, 115200, log)
            except TransportError:
                continue
            try:
                bl.identify()
                log(f"Found bootloader on {dev}: board {bl.board_id} rev {bl.board_rev}, "
                    f"bootloader v{bl.bl_rev}, {bl.flash_size // 1024} kB flash")
                return bl
            except (FirmwareError, TransportError):
                bl.close()
        time.sleep(0.5)
    raise FirmwareError("No board in bootloader mode was found. Unplug the board, press Install, "
                        "then plug it back in within 30 seconds.")


def flash(fw, bl, progress=None, log=None):
    log = log or (lambda s: None)
    if fw.board_id != bl.board_id and not (bl.board_id == 33 and fw.board_id == 9):
        raise FirmwareError(f"This firmware is for board {fw.board_id}, but the connected board is "
                            f"{bl.board_id}. Choose the right firmware for your board.")
    if bl.flash_size and len(fw.image) > bl.flash_size:
        raise FirmwareError(f"The firmware ({len(fw.image)} bytes) does not fit in this board's "
                            f"flash ({bl.flash_size} bytes).")
    bl.erase()
    log(f"Writing {len(fw.image) // 1024} kB…")
    bl.program(fw.image, progress)
    if bl.bl_rev >= 3:
        log("Verifying…")
        bl.verify(fw.image)
    log("Done. Rebooting the board.")
    bl.reboot()
