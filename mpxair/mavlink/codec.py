"""MAVLink v1/v2 packet encoding and decoding (no external dependencies)."""
import struct

from . import defs

STX_V1 = 0xFE
STX_V2 = 0xFD

_FMT = {
    "char": "s", "uint8_t": "B", "int8_t": "b", "uint16_t": "H", "int16_t": "h",
    "uint32_t": "I", "int32_t": "i", "float": "f", "uint64_t": "Q", "int64_t": "q",
    "double": "d",
}


def x25crc(data, crc=0xFFFF):
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


class MsgDef:
    __slots__ = ("id", "name", "crc_extra", "fields", "names", "struct", "size",
                 "base_size", "arrays", "chars", "enums", "units")

    def __init__(self, mid, name, crc_extra, fields):
        self.id = mid
        self.name = name
        self.crc_extra = crc_extra
        self.fields = fields
        self.names = [f[0] for f in fields]
        fmt = "<"
        self.arrays = {}
        self.chars = set()
        base = 0
        for fname, ftype, arr, ext, _enum, _units in fields:
            if ftype == "char":
                fmt += f"{arr or 1}s"
                self.chars.add(fname)
                n = arr or 1
            elif arr:
                fmt += f"{arr}{_FMT[ftype]}"
                self.arrays[fname] = arr
                n = arr * struct.calcsize(_FMT[ftype])
            else:
                fmt += _FMT[ftype]
                n = struct.calcsize(_FMT[ftype])
            if not ext:
                base += n
        self.struct = struct.Struct(fmt)
        self.size = self.struct.size
        self.base_size = base
        self.enums = {f[0]: f[4] for f in fields if f[4]}
        self.units = {f[0]: f[5] for f in fields if f[5]}


MSG_BY_ID = {mid: MsgDef(mid, *v) for mid, v in defs.MESSAGES.items()}
MSG_BY_NAME = {m.name: m for m in MSG_BY_ID.values()}


class Message:
    """A decoded MAVLink message. Fields are attributes."""

    def __init__(self, mdef, values, sysid=0, compid=0, seq=0):
        self._def = mdef
        self._sysid = sysid
        self._compid = compid
        self._seq = seq
        self._ts = 0.0
        self.__dict__.update(values)

    @property
    def msg_type(self):
        return self._def.name

    @property
    def msg_id(self):
        return self._def.id

    def get_srcSystem(self):
        return self._sysid

    def get_srcComponent(self):
        return self._compid

    def to_dict(self):
        return {n: getattr(self, n) for n in self._def.names}

    def __repr__(self):
        return f"{self._def.name} {self.to_dict()}"


def _unpack(mdef, payload):
    if len(payload) < mdef.size:
        payload = payload + bytes(mdef.size - len(payload))
    elif len(payload) > mdef.size:
        payload = payload[:mdef.size]
    raw = mdef.struct.unpack(payload)
    values = {}
    i = 0
    for fname, ftype, arr, _ext, _e, _u in mdef.fields:
        if ftype == "char":
            values[fname] = raw[i].split(b"\0", 1)[0].decode("utf-8", "replace")
            i += 1
        elif arr:
            values[fname] = list(raw[i:i + arr])
            i += arr
        else:
            values[fname] = raw[i]
            i += 1
    return values


def _pack(mdef, kwargs):
    vals = []
    for fname, ftype, arr, ext, _e, _u in mdef.fields:
        v = kwargs.get(fname)
        if ftype == "char":
            if v is None:
                v = b""
            if isinstance(v, str):
                v = v.encode("utf-8")
            vals.append(v)
        elif arr:
            v = list(v or [])
            v = v[:arr] + [0] * (arr - len(v))
            if ftype in ("float", "double"):
                v = [float(x) for x in v]
            else:
                v = [int(x) for x in v]
            vals.extend(v)
        else:
            if v is None:
                v = 0
            vals.append(float(v) if ftype in ("float", "double") else int(v))
    return mdef.struct.pack(*vals)


class Encoder:
    def __init__(self, sysid=255, compid=190, version=2):
        self.sysid = sysid
        self.compid = compid
        self.version = version
        self.seq = 0

    def encode(self, name, **kwargs):
        mdef = MSG_BY_NAME[name]
        payload = _pack(mdef, kwargs)
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFF
        sysid = kwargs.get("_sysid", self.sysid)
        compid = kwargs.get("_compid", self.compid)
        if self.version == 1 and mdef.id < 256:
            payload = payload[:mdef.base_size]
            hdr = struct.pack("<BBBBBB", STX_V1, len(payload), seq, sysid, compid, mdef.id)
        else:
            # v2 trims trailing zero bytes (keeping at least one)
            n = len(payload)
            while n > 1 and payload[n - 1] == 0:
                n -= 1
            payload = payload[:n]
            hdr = struct.pack("<BBBBBBB", STX_V2, len(payload), 0, 0, seq, sysid, compid)
            hdr += struct.pack("<I", mdef.id)[:3]
        crc = x25crc(hdr[1:] + payload)
        crc = x25crc(bytes([mdef.crc_extra]), crc)
        return hdr + payload + struct.pack("<H", crc)


class Parser:
    """Incremental byte-stream parser. feed() returns a list of decoded messages."""

    def __init__(self):
        self.buf = bytearray()
        self.crc_errors = 0
        self.unknown = 0
        self.received = 0
        self.last_seq = {}
        self.lost = 0
        self.v2_seen = False

    def feed(self, data):
        self.buf.extend(data)
        out = []
        buf = self.buf
        while True:
            # find start byte
            i = 0
            n = len(buf)
            while i < n and buf[i] != STX_V2 and buf[i] != STX_V1:
                i += 1
            if i:
                del buf[:i]
                n = len(buf)
            if n < 8:
                break
            if buf[0] == STX_V2:
                if n < 10:
                    break
                plen = buf[1]
                incompat = buf[2]
                total = 10 + plen + 2 + (13 if incompat & 1 else 0)
                if n < total:
                    break
                msgid = buf[7] | (buf[8] << 8) | (buf[9] << 16)
                hdr_end = 10
                seq, sysid, compid = buf[4], buf[5], buf[6]
            else:
                plen = buf[1]
                total = 6 + plen + 2
                if n < total:
                    break
                msgid = buf[5]
                hdr_end = 6
                seq, sysid, compid = buf[2], buf[3], buf[4]
            mdef = MSG_BY_ID.get(msgid)
            if mdef is None:
                # can't check CRC without crc_extra; skip this start byte cautiously
                self.unknown += 1
                del buf[:1]
                continue
            payload = bytes(buf[hdr_end:hdr_end + plen])
            crc = x25crc(bytes(buf[1:hdr_end]) + payload)
            crc = x25crc(bytes([mdef.crc_extra]), crc)
            got = buf[hdr_end + plen] | (buf[hdr_end + plen + 1] << 8)
            if crc != got:
                self.crc_errors += 1
                del buf[:1]
                continue
            if buf[0] == STX_V2:
                self.v2_seen = True
            raw = bytes(buf[:total])
            del buf[:total]
            try:
                values = _unpack(mdef, payload)
            except struct.error:
                continue
            key = (sysid, compid)
            last = self.last_seq.get(key)
            if last is not None:
                gap = (seq - last - 1) & 0xFF
                if gap < 128:
                    self.lost += gap
            self.last_seq[key] = seq
            self.received += 1
            m = Message(mdef, values, sysid, compid, seq)
            m._raw = raw
            out.append(m)
        return out
