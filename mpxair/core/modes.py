"""Vehicle type classification and ArduPilot flight-mode tables."""
from ..mavlink import defs

COPTER_TYPES = {2, 3, 4, 13, 14, 15, 29, 35}       # quad, coax, heli, hexa, octo, tri, dodeca, decarotor
PLANE_TYPES = {1, 16, 19, 20, 21, 22, 23, 24, 25}   # fixed wing + VTOL variants
ROVER_TYPES = {10, 11}                              # ground rover, surface boat
SUB_TYPES = {12}
TRACKER_TYPES = {5}

_ENUM_FOR = {
    "copter": "COPTER_MODE", "plane": "PLANE_MODE", "rover": "ROVER_MODE",
    "sub": "SUB_MODE", "tracker": "TRACKER_MODE",
}


_SHORT_NAMES = {"FLY_BY_WIRE_A": "FBWA", "FLY_BY_WIRE_B": "FBWB"}


def vehicle_class(mav_type):
    if mav_type in COPTER_TYPES:
        return "copter"
    if mav_type in PLANE_TYPES:
        return "plane"
    if mav_type in ROVER_TYPES:
        return "rover"
    if mav_type in SUB_TYPES:
        return "sub"
    if mav_type in TRACKER_TYPES:
        return "tracker"
    return "copter"


def mode_table(vclass):
    """Return {number: 'NAME'} for the vehicle class, from the MAVLink XML enums."""
    enum = defs.ENUMS.get(_ENUM_FOR.get(vclass, "COPTER_MODE"))
    if not enum:
        return {}
    out = {}
    for num, (name, _desc) in enum[1].items():
        prefix = name.split("_MODE_", 1)
        short = prefix[1] if len(prefix) == 2 else name
        out[num] = _SHORT_NAMES.get(short, short)
    return out


def mode_name(vclass, number):
    return mode_table(vclass).get(number, f"MODE {number}")


def mode_number(vclass, name):
    name = name.upper()
    for num, n in mode_table(vclass).items():
        if n == name:
            return num
    return None


# The modes that are useful in the quick "change mode" picker, per vehicle class.
COMMON_MODES = {
    "copter": ["STABILIZE", "ALT_HOLD", "LOITER", "POSHOLD", "AUTO", "GUIDED", "RTL",
               "SMART_RTL", "LAND", "BRAKE", "CIRCLE", "ACRO", "SPORT", "AUTOTUNE", "FOLLOW"],
    "plane": ["MANUAL", "STABILIZE", "FBWA", "FBWB", "CRUISE", "AUTO", "RTL", "LOITER",
              "GUIDED", "TAKEOFF", "CIRCLE", "ACRO", "TRAINING", "AUTOTUNE",
              "QSTABILIZE", "QHOVER", "QLOITER", "QLAND", "QRTL"],
    "rover": ["MANUAL", "ACRO", "STEERING", "HOLD", "LOITER", "AUTO", "RTL", "SMART_RTL",
              "GUIDED", "FOLLOW", "SIMPLE", "CIRCLE"],
    "sub": ["MANUAL", "STABILIZE", "ACRO", "ALT_HOLD", "POSHOLD", "AUTO", "GUIDED",
            "CIRCLE", "SURFACE"],
    "tracker": ["MANUAL", "STOP", "SCAN", "AUTO"],
}
