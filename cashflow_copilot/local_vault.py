"""Opt-in Windows DPAPI storage. No plaintext fallback or portable master key."""
import ctypes
import os
from pathlib import Path
import tempfile

from .errors import CashflowError


class VaultError(CashflowError):pass


class WindowsVault:
    available=os.name=='nt'
    def __init__(self,path):self.path=Path(path)
    def _crypt(self,data,protect):
        if not self.available:raise VaultError('Remember is unavailable on this OS. Use session-only settings.')
        if not isinstance(data,bytes) or len(data)>131072:raise VaultError('Local settings exceed the storage limit.')
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
        def blob(value):
            buffer=(ctypes.c_ubyte*len(value)).from_buffer_copy(value)
            return Blob(len(value),buffer),buffer
        source,source_buffer=blob(data);entropy,entropy_buffer=blob(b'Fluxora-local-connections-v1')
        result=Blob();crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
        if protect:
            fn=crypt.CryptProtectData
            fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
            args=(ctypes.byref(source),None,ctypes.byref(entropy),None,None,1,ctypes.byref(result))
        else:
            fn=crypt.CryptUnprotectData
            fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
            args=(ctypes.byref(source),None,ctypes.byref(entropy),None,None,1,ctypes.byref(result))
        fn.restype=wintypes.BOOL
        if not fn(*args):raise VaultError('Saved settings are locked or unavailable for this Windows account.')
        try:return ctypes.string_at(result.data,result.size)
        finally:kernel.LocalFree(ctypes.cast(result.data,ctypes.c_void_p))
    def load(self):
        if self.path.stat().st_size>131072:raise VaultError('Saved settings exceed the storage limit.')
        return self._crypt(self.path.read_bytes(),False)
    def save(self,data):
        encrypted=self._crypt(data,True)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent,prefix='.connections-',delete=False) as file:
                temporary=Path(file.name);os.chmod(temporary,0o600);file.write(encrypted);file.flush();os.fsync(file.fileno())
            os.replace(temporary,self.path);temporary=None
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
    def forget(self):self.path.unlink(missing_ok=True)
