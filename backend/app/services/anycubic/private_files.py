"""Private certificate files, adapted from the user-owned Anycubic Orca plugin."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from secrets import token_hex


class PrivateDirectory:
    def __init__(self):
        self.path = None
        self._files = []

    def __enter__(self):
        if os.name != "nt":
            self.path = Path(tempfile.mkdtemp(prefix="anycubic-tls-"))
            os.chmod(self.path, 0o700)
            return self
        import ctypes as c
        from ctypes import wintypes as w

        kernel = c.WinDLL("kernel32", use_last_error=True)
        advapi = c.WinDLL("advapi32", use_last_error=True)

        class SecurityAttributes(c.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]

        class SidAttributes(c.Structure):
            _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]

        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.LocalFree.argtypes, kernel.LocalFree.restype = [c.c_void_p], c.c_void_p
        advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
        advapi.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(c.c_wchar_p)]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            c.POINTER(c.c_void_p),
            c.POINTER(w.DWORD),
        ]
        kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, c.POINTER(SecurityAttributes)]
        token, descriptor, sid_string = w.HANDLE(), c.c_void_p(), c.c_wchar_p()
        try:
            if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, c.byref(token)):
                raise OSError("Cannot protect private TLS storage")
            size = w.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
            if not 0 < size.value < 65536:
                raise OSError("Cannot identify private TLS storage owner")
            info = c.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 1, info, size.value, c.byref(size)):
                raise OSError("Cannot identify private TLS storage owner")
            sid = c.cast(info, c.POINTER(SidAttributes)).contents.sid
            if not advapi.ConvertSidToStringSidW(sid, c.byref(sid_string)):
                raise OSError("Cannot identify private TLS storage owner")
            # Protected DACL; no inherited Everyone/Users/Authenticated Users grant.
            sddl = "D:P(A;OICI;FA;;;" + sid_string.value + ")(A;OICI;FA;;;SY)"
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
                raise OSError("Cannot protect private TLS storage")
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
            target = Path(tempfile.gettempdir()) / ("anycubic-tls-" + token_hex(24))
            if not kernel.CreateDirectoryW(str(target), c.byref(attributes)):
                raise OSError("Cannot create private TLS storage")
            self.path = target
            return self
        finally:
            if sid_string:
                kernel.LocalFree(c.cast(sid_string, c.c_void_p))
            if descriptor:
                kernel.LocalFree(descriptor)
            if token:
                kernel.CloseHandle(token)

    def write(self, name, value):
        if self.path is None or name not in {"client.pem", "client-key.pem"} or type(value) is not bytes:
            raise ValueError("Invalid private TLS file")
        path = self.path / name
        with path.open("xb") as stream:
            self._files.append(path)
            stream.write(value)
        return path

    def __exit__(self, *args):
        if self.path:
            for path in self._files:
                path.unlink(missing_ok=True)
            self.path.rmdir()
        self.path = None
