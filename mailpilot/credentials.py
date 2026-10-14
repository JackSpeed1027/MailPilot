"""Scoped Windows credentials; no secrets in JSON or page responses."""
import ctypes
import hashlib
import os
from ctypes import wintypes


class WindowsVault:
    def __init__(self):
        if os.name != 'nt':
            raise ValueError('记住密钥仅支持 Windows；请选择仅本次运行。')
        class Credential(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        self.kind = Credential
        self.api = ctypes.WinDLL('advapi32', use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(Credential))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL

    def read(self, target):
        pointer = ctypes.POINTER(self.kind)()
        if not self.api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return ''
            raise ValueError('无法读取 Windows 凭据，请重新输入或使用仅本次运行。')
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode('utf-8')
        finally:
            self.api.CredFree(pointer)

    def write(self, target, value):
        blob = value.encode('utf-8')
        if len(blob) > 2560:
            raise ValueError('密钥过长，Windows 凭据最多保存 2560 字节。')
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self.kind(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                               CredentialBlob=buffer, Persist=2, UserName='MailPilot')
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            raise ValueError('Windows 凭据保存失败；请使用仅本次运行。')

    def delete(self, target):
        if not self.api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise ValueError('Windows 凭据删除失败。')


def bindings(cfg):
    result = {}
    for account in cfg['accounts']:
        result[account['secret_env']] = 'imap:' + account['host'] + ':' + account['email']
    result[cfg['llm']['key_env']] = 'model:' + cfg['llm']['base_url'].rstrip('/')
    result[cfg['channel']['token_env']] = 'wxpusher'
    return result


class Credentials:
    def __init__(self, path, vault=None):
        self.prefix = 'MailPilot/' + hashlib.sha256(str(path).lower().encode()).hexdigest()[:24] + '/'
        self.session = {}
        self.vault = vault

    def target(self, name, binding):
        return self.prefix + hashlib.sha256((name + '\n' + binding).encode()).hexdigest()

    def get(self, name, binding):
        key = (name, binding)
        if key in self.session:
            return self.session[key]
        if self.vault:
            return self.vault.read(self.target(name, binding))
        return ''

    def put(self, name, binding, value, remember=False):
        if remember:
            if not self.vault:
                raise ValueError('当前无法访问 Windows 凭据，请使用仅本次运行。')
            self.vault.write(self.target(name, binding), value)
        else:
            # Choosing session-only must not leave an older persistent password behind.
            if self.vault:
                self.vault.delete(self.target(name, binding))
        self.session[name, binding] = value
