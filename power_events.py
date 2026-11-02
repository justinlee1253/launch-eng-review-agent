"""Observe macOS sleep/wake without power assertions, polling logs or extra tools."""
import ctypes as c
import sys
import threading


class Watcher:
    def __init__(self, sleep, wake):
        self.sleep, self.wake = sleep, wake
        self.stop, self.ready = threading.Event(), threading.Event()
        self.thread, self.error = None, None

    def dispatch(self, refcon, service, message, argument):
        # Apple requires acknowledgment for can-sleep and will-sleep, never wake.
        # https://developer.apple.com/library/archive/qa/qa1340/_index.html
        try:
            if message == 0xe0000280:
                self.sleep()
            elif message == 0xe0000300:
                self.wake()
        except Exception:
            pass
        finally:
            if message in (0xe0000270, 0xe0000280):
                self.io.IOAllowPowerChange(self.connection, argument)

    def start(self):
        if sys.platform != 'darwin':
            return
        self.thread = threading.Thread(target=self._run, name='orbit-power-events', daemon=True)
        self.thread.start()
        if not self.ready.wait(2) or self.error:
            self.close()
            raise OSError('macOS sleep/wake observer unavailable')

    def _run(self):
        port, notifier = c.c_void_p(), c.c_uint()
        self.connection = 0
        try:
            self.io = c.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
            cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            callback = c.CFUNCTYPE(None, c.c_void_p, c.c_uint, c.c_uint, c.c_void_p)(self.dispatch)
            signatures = (
                (self.io, 'IORegisterForSystemPower', [c.c_void_p, c.POINTER(c.c_void_p), type(callback), c.POINTER(c.c_uint)], c.c_uint),
                (self.io, 'IOAllowPowerChange', [c.c_uint, c.c_ssize_t], c.c_int),
                (self.io, 'IONotificationPortGetRunLoopSource', [c.c_void_p], c.c_void_p),
                (self.io, 'IODeregisterForSystemPower', [c.POINTER(c.c_uint)], c.c_int),
                (self.io, 'IONotificationPortDestroy', [c.c_void_p], None),
                (self.io, 'IOServiceClose', [c.c_uint], c.c_int),
                (cf, 'CFRunLoopGetCurrent', [], c.c_void_p),
                (cf, 'CFRunLoopAddSource', [c.c_void_p, c.c_void_p, c.c_void_p], None),
                (cf, 'CFRunLoopRemoveSource', [c.c_void_p, c.c_void_p, c.c_void_p], None),
                (cf, 'CFRunLoopRunInMode', [c.c_void_p, c.c_double, c.c_bool], c.c_int),
            )
            for library, name, args, result in signatures:
                function = getattr(library, name)
                function.argtypes, function.restype = args, result
            self.connection = self.io.IORegisterForSystemPower(None, c.byref(port), callback, c.byref(notifier))
            if not self.connection:
                raise OSError('Power notification registration failed')
            loop = cf.CFRunLoopGetCurrent()
            source = self.io.IONotificationPortGetRunLoopSource(port)
            if not source:
                raise OSError('Power notification source unavailable')
            mode = c.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
            cf.CFRunLoopAddSource(loop, source, mode)
            self.ready.set()
            try:
                while not self.stop.is_set():
                    cf.CFRunLoopRunInMode(mode, 1, False)
            finally:
                cf.CFRunLoopRemoveSource(loop, source, mode)
        except Exception:
            self.error = True
            self.ready.set()
        finally:
            if self.connection:
                self.io.IODeregisterForSystemPower(c.byref(notifier))
                self.io.IOServiceClose(self.connection)
            if port.value:
                self.io.IONotificationPortDestroy(port)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(4)
