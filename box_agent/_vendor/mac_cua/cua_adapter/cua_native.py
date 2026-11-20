"""Read-only AX window context supplement for the guest CUA adapter.

Runs in a bounded subprocess. This process needs its own macOS Accessibility
permission; the signed CuaDriver application's permission is not inherited.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import sys

ATTRIBUTES = {'focused_window': 'AXFocusedWindow', 'main_window': 'AXMainWindow'}


class MacAX:
    def __init__(self):
        self.ax = C.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        for lib, name, args, result in (
            (self.ax, 'AXIsProcessTrusted', [], C.c_bool),
            (self.ax, 'AXUIElementCreateApplication', [C.c_int], C.c_void_p),
            (self.ax, 'AXUIElementSetMessagingTimeout', [C.c_void_p, C.c_float], C.c_int),
            (self.ax, 'AXUIElementCopyAttributeValue', [C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)], C.c_int),
            (self.ax, 'AXUIElementGetTypeID', [], C.c_ulong),
            (self.ax, '_AXUIElementGetWindow', [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int),
            (self.cf, 'CFStringCreateWithCString', [C.c_void_p, C.c_char_p, C.c_uint32], C.c_void_p),
            (self.cf, 'CFGetTypeID', [C.c_void_p], C.c_ulong),
            (self.cf, 'CFBooleanGetTypeID', [], C.c_ulong),
            (self.cf, 'CFBooleanGetValue', [C.c_void_p], C.c_bool),
            (self.cf, 'CFRelease', [C.c_void_p], None),
        ):
            function = getattr(lib, name)
            function.argtypes, function.restype = args, result

    def read_window(self, app, attribute):
        key = self.cf.CFStringCreateWithCString(None, attribute.encode(), 0x08000100)
        if not key:
            return {'status': 'allocation_failed'}
        value = C.c_void_p()
        try:
            error = self.ax.AXUIElementCopyAttributeValue(app, key, C.byref(value))
            if error:
                return {'status': 'ax_error', 'ax_error': error}
            if not value.value:
                return {'status': 'no_value'}
            if self.cf.CFGetTypeID(value) != self.ax.AXUIElementGetTypeID():
                return {'status': 'unexpected_type'}
            window = C.c_uint32()
            error = self.ax._AXUIElementGetWindow(value, C.byref(window))
            if error or not window.value:
                return {'status': 'window_id_unresolved', 'ax_error': error}
            result = {'status': 'ok', 'window_id': window.value}
            minimized_key = self.cf.CFStringCreateWithCString(None, b'AXMinimized', 0x08000100)
            minimized = C.c_void_p()
            try:
                if (minimized_key and self.ax.AXUIElementCopyAttributeValue(
                        value, minimized_key, C.byref(minimized)) == 0
                        and minimized.value
                        and self.cf.CFGetTypeID(minimized) == self.cf.CFBooleanGetTypeID()):
                    result['minimized'] = bool(self.cf.CFBooleanGetValue(minimized))
            finally:
                if minimized.value:
                    self.cf.CFRelease(minimized)
                if minimized_key:
                    self.cf.CFRelease(minimized_key)
            return result
        finally:
            if value.value:
                self.cf.CFRelease(value)
            self.cf.CFRelease(key)

    def query(self, pid):
        if not self.ax.AXIsProcessTrusted():
            return {'status': 'permission_denied', 'trusted': False}
        app = self.ax.AXUIElementCreateApplication(pid)
        if not app:
            return {'status': 'application_unavailable', 'trusted': True}
        try:
            error = self.ax.AXUIElementSetMessagingTimeout(app, 1.0)
            if error:
                return {'status': 'ax_error', 'trusted': True, 'ax_error': error}
            windows = {key: self.read_window(app, attr) for key, attr in ATTRIBUTES.items()}
            return {'status': 'ok' if all(v['status'] == 'ok' for v in windows.values()) else 'partial',
                    'trusted': True, **windows}
        finally:
            self.cf.CFRelease(app)


def window_context(pid: int) -> dict:
    if type(pid) is not int or not 0 < pid < 2**31:
        raise ValueError('pid must be a positive signed 32-bit integer')
    result = {'source': 'guest_native_ax', 'pid': pid}
    if sys.platform != 'darwin':
        return {**result, 'status': 'unsupported_platform'}
    try:
        return {**result, **MacAX().query(pid)}
    except (OSError, AttributeError) as error:
        return {**result, 'status': 'native_api_unavailable', 'detail': str(error)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(window_context(args.pid)))
