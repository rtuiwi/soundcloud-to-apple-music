"""Read macOS thermal pressure without administrator rights or extra packages.

This is an OS pressure level, not a temperature measurement in Celsius.
"""
import ctypes
import json
import sys
import subprocess
from pathlib import Path

LABELS = {0: 'nominal', 1: 'fair', 2: 'serious', 3: 'critical'}

def _read_thermal_state():
    if sys.platform != 'darwin':
        return None
    try:
        ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        send_obj = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
        send_int = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(address)
        instance = send_obj(objc.objc_getClass(b'NSProcessInfo'), objc.sel_registerName(b'processInfo'))
        if not instance:
            return None
        state = send_int(instance, objc.sel_registerName(b'thermalState'))
        return state if state in LABELS else None
    except (OSError, AttributeError, ValueError):
        return None

def thermal_state():
    # NSProcessInfo may cache its state in a command-line process without a
    # Foundation event loop. A fresh short-lived reader avoids stale readings.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                                capture_output=True, text=True, timeout=3, check=True)
        state = json.loads(result.stdout).get('thermal_state')
        return state if state in LABELS else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

if __name__ == '__main__':
    state = _read_thermal_state()
    print(json.dumps({'thermal_state': state, 'thermal_label': LABELS.get(state, 'unknown'), 'temperature_celsius': None}))
