"""Per-launch local authentication; standard library, no listening service.

Windows session files contain CurrentUser DPAPI ciphertext, never plaintext
tokens. POSIX session files use owner-only permissions. No cookies are used:
cookies would also be sent to unrelated servers on other localhost ports.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser

TOKEN_PATTERN = r"[A-Za-z0-9_-]{43}"
MEDIA_PATTERN = r"/api/(?:media/[a-f0-9]{32}|audio/[a-f0-9-]{16,64}\.wav)"
MEDIA_TTL = 12 * 60 * 60
_ENTROPY = b"YorimichiRadio/local-session/v1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _dpapi(data, *, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(_ENTROPY)
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN only. LOCAL_MACHINE must never be used for this secret.
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(destination)):
        raise OSError("このWindows利用者の起動情報を読み書きできません。起動用ファイルから開き直してください。")
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(destination.data)


def session_path(data_dir, port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid radio port")
    return Path(data_dir) / f"session-{port}.json"


def save_session(data_dir, port, token, instance):
    if not re.fullmatch(TOKEN_PATTERN, token) or not re.fullmatch(TOKEN_PATTERN, instance):
        raise ValueError("Invalid session data")
    target = session_path(data_dir, port)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"token": token, "instance": instance, "port": port}).encode("ascii")
    if os.name == "nt":
        envelope = {"protection": "windows-current-user-dpapi", "payload": base64.b64encode(_dpapi(payload)).decode("ascii")}
    else:
        envelope = {"protection": "owner-only", "payload": base64.b64encode(payload).decode("ascii")}
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=target.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(envelope, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def load_session(data_dir, port):
    target = session_path(data_dir, port)
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
        raise ValueError("Invalid session file")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError("The radio session must be owned and readable only by the current user")
    envelope = json.loads(target.read_text(encoding="utf-8"))
    encoded = base64.b64decode(envelope["payload"], validate=True)
    if os.name == "nt":
        if envelope.get("protection") != "windows-current-user-dpapi":
            raise ValueError("Unprotected Windows session file")
        encoded = _dpapi(encoded, decrypt=True)
    elif envelope.get("protection") != "owner-only":
        raise ValueError("Invalid session protection")
    session = json.loads(encoded)
    if (session.get("port") != port or not re.fullmatch(TOKEN_PATTERN, session.get("token", ""))
            or not re.fullmatch(TOKEN_PATTERN, session.get("instance", ""))):
        raise ValueError("Invalid session data")
    return session


def remove_session(data_dir, port, instance):
    try:
        if load_session(data_dir, port)["instance"] == instance:
            session_path(data_dir, port).unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError):
        pass


class LocalAuth:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_urlsafe(32)

    def accepts(self, authorization):
        return (isinstance(authorization, str)
                and re.fullmatch("Bearer " + TOKEN_PATTERN, authorization) is not None
                and hmac.compare_digest(authorization[7:], self.token))

    def browser_url(self, port):
        return f"http://127.0.0.1:{port}/#session={self.token}"

    def _signature(self, path, expires):
        payload = (self.instance + "\n" + path + "\n" + str(expires)).encode("ascii")
        return base64.urlsafe_b64encode(hmac.digest(self.token.encode("ascii"), payload, "sha256")).decode("ascii").rstrip("=")

    def media_url(self, path):
        if not isinstance(path, str) or not re.fullmatch(MEDIA_PATTERN, path):
            raise ValueError("再生する音声の指定が不正です。")
        expires = int(time.time()) + MEDIA_TTL
        return path + "?" + urllib.parse.urlencode({"expires": expires, "signature": self._signature(path, expires)})

    def accepts_media(self, path, query):
        if not re.fullmatch(MEDIA_PATTERN, path):
            return False
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        if set(values) != {"expires", "signature"} or any(len(v) != 1 for v in values.values()):
            return False
        expiry = values["expires"][0]
        signature = values["signature"][0]
        if not re.fullmatch(r"[0-9]{1,12}", expiry) or not re.fullmatch(TOKEN_PATTERN, signature):
            return False
        expires, now = int(expiry), int(time.time())
        return (now <= expires <= now + MEDIA_TTL
                and hmac.compare_digest(signature, self._signature(path, expires)))


def main():
    parser = argparse.ArgumentParser(description="この利用者のラジオを開く・停止する")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--open-browser", action="store_true")
    action.add_argument("--shutdown", action="store_true")
    args = parser.parse_args()
    try:
        session = load_session(args.data_dir, args.port)
        base = f"http://127.0.0.1:{args.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(base + "/api/health", timeout=3) as response:
            body = response.read(16385)
            if len(body) > 16384:
                raise ValueError("Invalid radio health response")
            health = json.loads(body)
        if health.get("app") != "ai-radio" or health.get("auth_instance") != session["instance"]:
            raise ValueError("Session does not match this radio")
        if args.open_browser:
            if not webbrowser.open(base + "/#session=" + session["token"]):
                raise RuntimeError("Browser could not be opened")
        else:
            request = urllib.request.Request(base + "/api/shutdown", data=b"{}",
                headers={"Origin": base, "Content-Type": "application/json", "Authorization": "Bearer " + session["token"]})
            with opener.open(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("Shutdown was rejected")
        return 0
    except Exception:
        # Never echo a token, URL fragment, encrypted blob or request headers.
        print("起動情報を確認できません。同じWindows利用者で起動用ファイルから開き直してください。旧版が動作中なら、旧版の停止用ファイルで停止してから起動してください。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
