"""Durable, coalescing event queue and routing-netlink event filtering."""
import os
from pathlib import Path
import socket
import struct
import stat
import time


class EventQueue:
    def __init__(self, directory, debounce=3, recover=True):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022):
            raise PermissionError('IPv6 queue must be an owned directory without group/world write access')
        self._token = self._deadline = None
        self.pending = self.directory / 'pending'
        self.inflight = self.directory / 'inflight'
        self.debounce = debounce
        if recover and self.inflight.exists():
            self.notify()
            self.inflight.unlink()
            self.sync()

    def sync(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def notify(self, now=None):
        try:
            fd = os.open(self.pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        try:
            if now is not None:
                os.utime(fd, (now, now))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sync()

    def ready(self, now=None):
        try:
            metadata = self.pending.stat()
        except FileNotFoundError:
            return False
        if now is not None:
            return now >= metadata.st_mtime + self.debounce
        token = (metadata.st_ino, metadata.st_ctime_ns)
        if token != self._token:
            self._token = token
            age = max(0, min(self.debounce, time.time() - metadata.st_mtime))
            self._deadline = time.monotonic() + self.debounce - age
        return time.monotonic() >= self._deadline

    def claim(self):
        self.pending.replace(self.inflight)
        self.sync()

    def finish(self, success):
        if not success:
            self.notify()
        self.inflight.unlink(missing_ok=True)
        self.sync()


def attributes(data, offset):
    result = {}
    while offset + 4 <= len(data):
        size, kind = struct.unpack_from('HH', data, offset)
        if size < 4 or offset + size > len(data):
            break
        result[kind & 0x3fff] = data[offset + 4:offset + size]
        offset += (size + 3) & ~3
    return result


class NetlinkFilter:
    def __init__(self):
        self.state = {}

    def changed(self, data):
        data = bytes(data)
        changed = False
        offset = 0
        while offset + 16 <= len(data):
            length, kind, _, _, _ = struct.unpack_from('IHHII', data, offset)
            if length < 16 or offset + length > len(data):
                return True  # Resynchronize after truncated/invalid delivery.
            body = data[offset + 16:offset + length]
            offset += (length + 3) & ~3
            if kind in (2, 4):  # NLMSG_ERROR, NLMSG_OVERRUN
                changed = True
                continue
            key = signature = None
            deleting = kind in (17, 21, 25)
            if kind in (20, 21) and len(body) >= 8 and body[0] == socket.AF_INET6:
                family, prefix, flags, scope, index = struct.unpack_from('BBBBI', body)
                attrs = attributes(body, 8)
                if 8 in attrs and len(attrs[8]) == 4:
                    flags = struct.unpack('I', attrs[8])[0]
                # Temporary privacy addresses are not consumed by netquery6.
                if flags & 1:
                    continue
                address = attrs.get(2, attrs.get(1, b''))
                key = ('address', index, address, prefix)
                signature = (scope, flags)
            elif kind in (24, 25) and len(body) >= 12 and body[0] == socket.AF_INET6:
                if struct.unpack_from('I', body, 8)[0] & 0x200:  # RTM_F_CLONED
                    continue
                attrs = attributes(body, 12)
                attrs.pop(23, None)  # RTA_EXPIRES is a countdown, not a route change.
                attrs.pop(12, None)  # RTA_CACHEINFO lifetimes are not configuration.
                signature = (body[:12], tuple(sorted(attrs.items())))
                key = ('route', signature)
            elif kind in (16, 17) and len(body) >= 16:
                _, _, _, index, flags, _ = struct.unpack_from('BBHiII', body)
                attrs = attributes(body, 16)
                key = ('link', index)
                signature = (flags & (1 | 0x40 | 0x10000), attrs.get(3))
            if key is None:
                continue
            if deleting:
                self.state.pop(key, None)
                changed = True
            else:
                if self.state.get(key) != signature:
                    changed = True
                self.state[key] = signature
        return changed
