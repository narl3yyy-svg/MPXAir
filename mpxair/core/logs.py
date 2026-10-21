"""Logs: onboard DataFlash download over MAVLink, .bin parsing, .tlog reading and replay."""
import os
import struct
import threading
import time

from ..mavlink.codec import Parser

# ----------------------------------------------------------- DataFlash .bin
HEAD1, HEAD2 = 0xA3, 0x95
FMT_TYPE = 128

_DF_TYPES = {
    "a": ("32h", None), "b": ("b", None), "B": ("B", None), "h": ("h", None), "H": ("H", None),
    "i": ("i", None), "I": ("I", None), "f": ("f", None), "d": ("d", None), "n": ("4s", "str"),
    "N": ("16s", "str"), "Z": ("64s", "str"), "c": ("h", 0.01), "C": ("H", 0.01), "e": ("i", 0.01),
    "E": ("I", 0.01), "L": ("i", 1e-7), "M": ("B", None), "q": ("q", None), "Q": ("Q", None),
    "g": ("e", None),
}


class DFFormat:
    __slots__ = ("type", "name", "length", "fmt", "columns", "struct", "scales", "ok")

    def __init__(self, mtype, name, length, fmt, columns):
        self.type = mtype
        self.name = name
        self.length = length
        self.fmt = fmt
        self.columns = columns
        self.scales = []
        sfmt = "<"
        self.ok = True
        for ch in fmt:
            t = _DF_TYPES.get(ch)
            if t is None:
                self.ok = False
                break
            sfmt += t[0]
            self.scales.append(t[1])
        if self.ok:
            self.struct = struct.Struct(sfmt)
            if self.struct.size != length - 3:
                self.ok = False


class DFLog:
    """Whole log in memory: per message name a list of field columns."""

    def __init__(self):
        self.formats = {}
        self.data = {}          # name -> {"cols": [names], "rows": [tuples]}
        self.messages = []      # (time_s, text)
        self.modes = []         # (time_s, mode_number)
        self.params = {}
        self.t0 = None
        self.duration = 0.0
        self.path = None

    def series(self, name, field):
        d = self.data.get(name)
        if not d or field not in d["cols"]:
            return [], []
        ti = d["cols"].index("TimeUS") if "TimeUS" in d["cols"] else None
        fi = d["cols"].index(field)
        xs, ys = [], []
        t0 = self.t0 or 0
        for r in d["rows"]:
            v = r[fi]
            if isinstance(v, (int, float)):
                xs.append(((r[ti] - t0) / 1e6) if ti is not None else len(xs))
                ys.append(v)
        return xs, ys

    def gps_track(self):
        d = self.data.get("GPS") or self.data.get("POS")
        if not d:
            return []
        cols = d["cols"]
        la = "Lat" if "Lat" in cols else None
        lo = "Lng" if "Lng" in cols else ("Lon" if "Lon" in cols else None)
        if not la or not lo:
            return []
        i, j = cols.index(la), cols.index(lo)
        return [(r[i], r[j]) for r in d["rows"] if r[i] and r[j]]


def parse_bin(path, progress=None, cancel=None):
    with open(path, "rb") as fh:
        buf = fh.read()
    log = DFLog()
    log.path = path
    n = len(buf)
    pos = 0
    fmts = log.formats
    fmts[FMT_TYPE] = DFFormat(FMT_TYPE, "FMT", 89, "BBnNZ", ["Type", "Length", "Name", "Format", "Columns"])
    last_report = 0
    first_t = None
    last_t = 0
    unpack_fmt = struct.Struct("<BB4s16s64s")
    while pos < n - 3:
        if buf[pos] != HEAD1 or buf[pos + 1] != HEAD2:
            nxt = buf.find(b"\xa3\x95", pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        mtype = buf[pos + 2]
        f = fmts.get(mtype)
        if f is None:
            pos += 1
            continue
        end = pos + f.length
        if end > n:
            break
        if mtype == FMT_TYPE:
            t, ln, name, fm, cols = unpack_fmt.unpack_from(buf, pos + 3)
            name = name.rstrip(b"\0").decode("ascii", "replace")
            fm = fm.rstrip(b"\0").decode("ascii", "replace")
            cols = cols.rstrip(b"\0").decode("ascii", "replace").split(",")
            nf = DFFormat(t, name, ln, fm, cols)
            if t != FMT_TYPE:
                fmts[t] = nf
                if nf.ok:
                    log.data.setdefault(name, {"cols": cols, "rows": []})
            pos = end
            continue
        if not f.ok:
            pos = end
            continue
        try:
            raw = f.struct.unpack_from(buf, pos + 3)
        except struct.error:
            pos += 1
            continue
        vals = []
        k = 0
        for ch, sc in zip(f.fmt, f.scales):
            if ch == "a":
                vals.append(raw[k:k + 32])
                k += 32
                continue
            v = raw[k]
            k += 1
            if sc == "str":
                v = v.rstrip(b"\0").decode("ascii", "replace")
            elif sc:
                v = v * sc
            vals.append(v)
        row = tuple(vals)
        log.data[f.name]["rows"].append(row)
        if f.columns and f.columns[0] == "TimeUS" and isinstance(row[0], int):
            if first_t is None and row[0] > 0:
                first_t = row[0]
            last_t = max(last_t, row[0])
        if f.name == "MSG":
            ci = f.columns.index("Message") if "Message" in f.columns else -1
            if ci >= 0:
                log.messages.append((row[0], row[ci]))
        elif f.name == "MODE":
            ci = f.columns.index("Mode") if "Mode" in f.columns else (f.columns.index("ModeNum") if "ModeNum" in f.columns else -1)
            if ci >= 0:
                log.modes.append((row[0], row[ci]))
        elif f.name == "PARM":
            if "Name" in f.columns and "Value" in f.columns:
                log.params[row[f.columns.index("Name")]] = row[f.columns.index("Value")]
        pos = end
        if progress and pos - last_report > 2_000_000:
            last_report = pos
            progress(pos, n)
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled")
    log.t0 = first_t or 0
    log.duration = (last_t - log.t0) / 1e6 if first_t else 0.0
    log.messages = [((t - log.t0) / 1e6, m) for t, m in log.messages]
    log.modes = [((t - log.t0) / 1e6, m) for t, m in log.modes]
    log.data = {k: v for k, v in log.data.items() if v["rows"]}
    if progress:
        progress(n, n)
    return log


# ------------------------------------------------------- onboard log download
class LogDownloader:
    def __init__(self, vehicle):
        self.v = vehicle

    def list(self, timeout=5.0):
        """Returns [(id, size, time_utc)]. Worker thread only."""
        v = self.v
        entries = {}
        done = threading.Event()
        total = [None]

        def on_entry(m):
            if m.num_logs == 0:
                total[0] = 0
                done.set()
                return
            entries[m.id] = (m.id, m.size, m.time_utc)
            total[0] = m.num_logs
            if len(entries) >= m.num_logs:
                done.set()

        h = v.on("LOG_ENTRY", on_entry)
        try:
            for _attempt in range(3):
                v.send("LOG_REQUEST_LIST", **v.target, start=0, end=0xFFFF)
                if done.wait(timeout):
                    break
                if entries:
                    # some arrived: ask again for the rest
                    continue
        finally:
            v.off(h)
        if total[0] is None and not entries:
            raise RuntimeError("The vehicle did not answer the log list request.")
        return sorted(entries.values())

    def download(self, log_id, size, path, progress=None, cancel=None):
        """Download one log to path with gap filling. Worker thread only."""
        v = self.v
        block = 90
        nblocks = (size + block - 1) // block if size else 0
        have = bytearray(nblocks) if nblocks else bytearray()
        data = bytearray(size)
        state = {"last": time.monotonic(), "end": size if size else None, "got": 0}
        lock = threading.Lock()

        def on_data(m):
            if m.id != log_id:
                return
            with lock:
                state["last"] = time.monotonic()
                if m.count == 0 or (m.count < block and state["end"] is None):
                    state["end"] = m.ofs + m.count
                if m.count:
                    end = m.ofs + m.count
                    if end > len(data):
                        data.extend(bytes(end - len(data)))
                    data[m.ofs:end] = bytes(m.data[:m.count])
                    bi = m.ofs // block
                    while bi >= len(have):
                        have.append(0)
                    if not have[bi]:
                        have[bi] = 1
                        state["got"] += m.count

        h = v.on("LOG_DATA", on_data)
        try:
            v.send("LOG_REQUEST_DATA", **v.target, id=log_id, ofs=0, count=0xFFFFFFFF)
            started = time.monotonic()
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Cancelled")
                time.sleep(0.1)
                with lock:
                    end = state["end"] if state["end"] is not None else size
                    nb = (end + block - 1) // block if end else 0
                    missing = [i for i in range(nb) if i >= len(have) or not have[i]]
                    idle = time.monotonic() - state["last"]
                    got = state["got"]
                if progress:
                    progress(min(got, end or got), end or size)
                if end and not missing:
                    break
                if idle > 0.5:
                    if not missing:
                        if time.monotonic() - started > 10 and not got:
                            raise RuntimeError("The vehicle sent no log data.")
                        continue
                    # request the first gap
                    a = missing[0]
                    b = a
                    while b + 1 < nb and (b + 1 >= len(have) or not have[b + 1]) and b - a < 200:
                        b += 1
                    v.send("LOG_REQUEST_DATA", **v.target, id=log_id, ofs=a * block,
                           count=(b - a + 1) * block)
                    with lock:
                        state["last"] = time.monotonic()
                    if idle > 15:
                        raise RuntimeError("The download stalled.")
        finally:
            v.off(h)
            v.send("LOG_REQUEST_END", **v.target)
        end = state["end"] if state["end"] is not None else size
        with open(path, "wb") as fh:
            fh.write(bytes(data[:end]))
        return end

    def erase(self):
        for _ in range(2):
            self.v.send("LOG_ERASE", **self.v.target)
            time.sleep(0.2)


# ---------------------------------------------------------------- .tlog files
def read_tlog(path):
    """Yield (timestamp_seconds, raw_bytes, Message) for every packet in a .tlog."""
    with open(path, "rb") as fh:
        buf = fh.read()
    p = Parser()
    pos = 0
    n = len(buf)
    while pos + 8 < n:
        ts = struct.unpack_from(">Q", buf, pos)[0]
        start = pos + 8
        if start >= n:
            break
        stx = buf[start]
        if stx == 0xFD and start + 10 <= n:
            ln = 12 + buf[start + 1] + (13 if buf[start + 2] & 1 else 0)
        elif stx == 0xFE and start + 6 <= n:
            ln = 8 + buf[start + 1]
        else:
            pos += 1
            continue
        raw = buf[start:start + ln]
        msgs = p.feed(raw)
        for m in msgs:
            yield ts / 1e6, raw, m
        pos = start + ln


class TlogTransport:
    """A replay 'link': feeds a .tlog through the normal Vehicle pipeline in real time."""
    kind = "tlog"

    def __init__(self, path, speed=1.0):
        self.name = f"Replay {os.path.basename(path)}"
        self.packets = []
        self.path = path
        last_ts = None
        for ts, raw, m in read_tlog(path):
            # only replay what the vehicle sent (skip our own GCS packets)
            if m.get_srcSystem() == 255:
                continue
            self.packets.append((ts, raw))
            last_ts = ts
        if not self.packets:
            raise ValueError("This telemetry log has no vehicle data.")
        self.t_start = self.packets[0][0]
        self.duration = (last_ts or self.t_start) - self.t_start
        self.speed = speed
        self.paused = False
        self.index = 0
        self.closed = False
        self._clock = time.monotonic()
        self._log_t = self.t_start

    @property
    def position(self):
        return self._log_t - self.t_start

    def seek(self, seconds):
        target = self.t_start + max(0.0, min(self.duration, seconds))
        lo, hi = 0, len(self.packets)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.packets[mid][0] < target:
                lo = mid + 1
            else:
                hi = mid
        self.index = lo
        self._log_t = target
        self._clock = time.monotonic()

    def read(self, timeout=0.1):
        time.sleep(min(timeout, 0.02))
        if self.closed:
            from .transport import TransportError
            raise TransportError("Replay closed")
        now = time.monotonic()
        if not self.paused:
            self._log_t += (now - self._clock) * self.speed
        self._clock = now
        out = bytearray()
        while self.index < len(self.packets) and self.packets[self.index][0] <= self._log_t:
            out += self.packets[self.index][1]
            self.index += 1
        if self.index >= len(self.packets):
            self.paused = True
        return bytes(out)

    def write(self, data):
        pass

    def close(self):
        self.closed = True
