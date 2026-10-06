"""Small, dependency-free credential storage helpers for AnyCodex.

Windows uses Credential Manager (generic credentials). Other platforms keep
using environment variables; this module deliberately does not invent a
plaintext cross-platform secret store.
"""
import ctypes
import os
import sys


TARGET_PREFIX = "AnyCodex/"


def _target(section):
    return TARGET_PREFIX + section


if sys.platform == "win32":
    from ctypes import wintypes

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_PCREDENTIALW),
    ]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]


def read_credential(section):
    """Return a vendor key from Windows Credential Manager, or None."""
    if sys.platform != "win32":
        return None
    ptr = _PCREDENTIALW()
    if not _advapi32.CredReadW(_target(section), 1, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-8") if raw else None
    finally:
        _advapi32.CredFree(ptr)


def write_credential(section, value):
    """Persist a vendor key in Windows Credential Manager."""
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is unavailable on this platform")
    raw = value.encode("utf-8")
    blob = ctypes.create_string_buffer(raw)
    cred = _CREDENTIALW()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _target(section)
    cred.CredentialBlobSize = len(raw)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE (for this Windows user)
    cred.UserName = "AnyCodex"
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_credential(section):
    """Delete an AnyCodex credential; missing credentials are harmless."""
    if sys.platform != "win32":
        return False
    if _advapi32.CredDeleteW(_target(section), 1, 0):
        return True
    # ERROR_NOT_FOUND
    if ctypes.get_last_error() == 1168:
        return False
    raise ctypes.WinError(ctypes.get_last_error())
