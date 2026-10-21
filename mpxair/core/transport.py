"""Byte transports for MAVLink links: serial, UDP and TCP.

Serial uses termios directly so there is no dependency on pyserial.
Every transport exposes read(timeout) -> bytes, write(bytes), close().
"""
import glob
import os
import re
import select
import socket
import termios
import time

BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 500000, 921600, 1500000]


def list_serial_ports():
    """Return [(device, description)] for likely flight-controller / radio ports."""
    ports = []
    by_id = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            by_id[os.path.realpath(link)] = os.path.basename(link)
        except OSError:
            pass
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/ttyAMA*", "/dev/ttyTHS*", "/dev/rfcomm*"):
        for dev in sorted(glob.glob(pat)):
            desc = by_id.get(dev, "")
            desc = re.sub(r"-if\d+(-port\d+)?$", "", desc.replace("usb-", ""))
            # drop the trailing serial number (e.g. _AA:BB:CC:DD:EE:FF or _A1B2C3D4)
            desc = re.sub(r"_[0-9A-Fa-f:]{6,}$", "", desc).replace("_", " ")
            ports.append((dev, desc))
    return ports


def usb_ids(device):
    """(vendor_id, product_id) as hex strings for a /dev/tty* USB device, or (None, None)."""
    name = os.path.basename(os.path.realpath(device))
    path = os.path.realpath(f"/sys/class/tty/{name}/device")
    for _ in range(5):
        vid = os.path.join(path, "idVendor")
        if os.path.exists(vid):
            try:
                with open(vid) as fh:
                    v = fh.read().strip()
                with open(os.path.join(path, "idProduct")) as fh:
                    return v, fh.read().strip()
            except OSError:
                return None, None
        path = os.path.dirname(path)
    return None, None


def is_espressif(device):
    return usb_ids(device)[0] == "303a"


class TransportError(Exception):
    pass


class SerialTransport:
    kind = "serial"

    def __init__(self, device, baud=115200):
        self.device = device
        self.baud = baud
        self.name = f"{device} @ {baud}"
        try:
            self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            raise TransportError(
                f"Permission denied opening {device}. Your user needs to be in the "
                "'dialout' group (the installer can set this up), then log out and back in.")
        except OSError as e:
            raise TransportError(f"Could not open {device}: {e.strerror}")
        self._configure(baud)
        self.espressif = is_espressif(device)
        if self.espressif:
            # ESP32 boards reset (or drop into their bootloader) on some DTR/RTS
            # patterns. Linux raises both on open; release them like the board's
            # own settings page does: RTS first, then DTR.
            self.set_rts(False)
            self.set_dtr(False)

    def _configure(self, baud):
        attrs = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{baud}", None)
        if speed is None:
            os.close(self.fd)
            raise TransportError(f"Baud rate {baud} is not supported")
        # raw mode, 8N1, no flow control
        attrs[0] = 0                                     # iflag
        attrs[1] = 0                                     # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                     # lflag
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def set_baud(self, baud):
        self.baud = baud
        self._configure(baud)

    def _modem_line(self, bit, on):
        import fcntl
        import struct
        TIOCMBIS, TIOCMBIC = 0x5416, 0x5417
        try:
            fcntl.ioctl(self.fd, TIOCMBIS if on else TIOCMBIC, struct.pack("I", bit))
        except OSError:
            pass

    def set_dtr(self, on):
        self._modem_line(0x002, on)

    def set_rts(self, on):
        self._modem_line(0x004, on)

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise TransportError(f"Serial port error: {e.strerror}")
        if not data:
            # select said readable but no data: device unplugged
            raise TransportError("Serial device disconnected")
        return data

    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 1.0
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TransportError("Serial write timed out")
                select.select([], [self.fd], [], 0.05)
            except OSError as e:
                raise TransportError(f"Serial port error: {e.strerror}")

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class UdpServerTransport:
    """Listen on a UDP port; reply to whoever last sent us data (like MP's UDP mode)."""
    kind = "udp"

    def __init__(self, port=14550, host="0.0.0.0"):
        self.name = f"UDP :{port}"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind((host, int(port)))
        except OSError as e:
            raise TransportError(f"Could not listen on UDP port {port}: {e.strerror}")
        self.sock.setblocking(False)
        self.peer = None

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        try:
            data, addr = self.sock.recvfrom(65535)
        except (BlockingIOError, ConnectionRefusedError):
            return b""
        self.peer = addr
        return data

    def write(self, data):
        if self.peer:
            try:
                self.sock.sendto(data, self.peer)
            except OSError:
                pass

    def close(self):
        self.sock.close()


class UdpClientTransport:
    kind = "udpci"

    def __init__(self, host, port):
        self.name = f"UDP {host}:{port}"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        try:
            self.peer = (socket.gethostbyname(host), int(port))
        except OSError as e:
            raise TransportError(f"Unknown host {host}: {e}")

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        try:
            data, _ = self.sock.recvfrom(65535)
            return data
        except (BlockingIOError, ConnectionRefusedError):
            return b""

    def write(self, data):
        try:
            self.sock.sendto(data, self.peer)
        except OSError:
            pass

    def close(self):
        self.sock.close()


class TcpTransport:
    kind = "tcp"

    def __init__(self, host, port):
        self.name = f"TCP {host}:{port}"
        try:
            self.sock = socket.create_connection((host, int(port)), timeout=5)
        except OSError as e:
            raise TransportError(f"Could not connect to {host}:{port}: {e.strerror or e}")
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setblocking(False)

    def read(self, timeout=0.1):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        try:
            data = self.sock.recv(65535)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise TransportError(f"TCP connection error: {e.strerror}")
        if not data:
            raise TransportError("TCP connection closed by the other side")
        return data

    def write(self, data):
        try:
            self.sock.setblocking(True)
            self.sock.sendall(data)
        except OSError as e:
            raise TransportError(f"TCP connection error: {e.strerror}")
        finally:
            try:
                self.sock.setblocking(False)
            except OSError:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def open_transport(spec):
    """spec: dict with 'type' in serial/udp/udpout/tcp plus fields."""
    t = spec.get("type")
    if t == "serial":
        return SerialTransport(spec["device"], int(spec.get("baud", 115200)))
    if t == "udp":
        return UdpServerTransport(int(spec.get("port", 14550)))
    if t == "udpout":
        return UdpClientTransport(spec.get("host", "127.0.0.1"), int(spec.get("port", 14550)))
    if t == "tcp":
        return TcpTransport(spec.get("host", "127.0.0.1"), int(spec.get("port", 5760)))
    if t == "tlog":
        from .logs import TlogTransport
        try:
            return TlogTransport(spec["path"], float(spec.get("speed", 1.0)))
        except (OSError, ValueError) as e:
            raise TransportError(f"Can't replay this file: {e}")
    raise TransportError(f"Unknown connection type {t}")
