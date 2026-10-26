"""One shared, non-destructive byte budget for pilot private sources and PDFs.

All API/worker writers for a root must use this adapter. A cross-process flock
serializes counting and immutable publication. Expired records do not delete
files: an operator must arrange retention after the approved pilot. This is a
single-host Linux adapter, not a distributed quota or production object store.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from services.whatsapp_forecast import ForecastNeedsCorrection
from services.whatsapp_store import PrivateFileStore

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_FREE_RESERVE_BYTES = 128 * 1024 * 1024
MAX_OBJECT_BYTES = 10 * 1024 * 1024
_LOCK = ".quota.lock"
_NAMESPACE = re.compile(r"[a-f0-9]{64}\Z")
_OBJECT = re.compile(r"[a-f0-9]{64}\.(pdf|png|jpg)\Z")
_PENDING = re.compile(r"\.pending-[a-f0-9]{32}\Z")
_MESSAGE = "Private storage quota or safety limit reached. Contact the operator; do not repeatedly resend the file."


class PrivateStorageQuotaError(ForecastNeedsCorrection):
    """Safe failure: no paths, source data, credentials or disk details exposed."""
    def __init__(self):
        super().__init__(_MESSAGE)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PrivateStorageQuotaError()


def _directory(path, *, create=False):
    """Open/create each absolute component relative to its no-symlink parent FD."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(path).parts[1:]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


class QuotaPrivateFileStore(PrivateFileStore):
    def __init__(self, root, *, max_bytes=DEFAULT_MAX_BYTES, free_reserve_bytes=DEFAULT_FREE_RESERVE_BYTES):
        if type(max_bytes) is not int or max_bytes < 1 or type(free_reserve_bytes) is not int or free_reserve_bytes < 0:
            raise ValueError("Explicit positive storage budget and nonnegative free reserve are required")
        self.max_bytes, self.free_reserve_bytes = max_bytes, free_reserve_bytes
        # Do not resolve symlinks before checking them, unlike a generic Path.resolve.
        path = Path(os.path.abspath(os.fspath(root)))
        try:
            self.root = path
            fd = _directory(path, create=True)
            try:
                os.fchmod(fd, 0o700)
                info = os.fstat(fd)
                self._root_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
        except OSError:
            raise PrivateStorageQuotaError() from None

    @contextmanager
    def _root(self):
        fd = None
        try:
            fd = _directory(self.root)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise PrivateStorageQuotaError()
            yield fd
        except OSError:
            raise PrivateStorageQuotaError() from None
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def _locked(self):
        with self._root() as root_fd:
            lock_fd = None
            try:
                lock_fd = os.open(_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                _regular(os.fstat(lock_fd))
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                info = os.fstat(lock_fd)
                named = os.stat(_LOCK, dir_fd=root_fd, follow_symlinks=False)
                _regular(info)
                _regular(named)
                if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                    raise PrivateStorageQuotaError()
                yield root_fd
            except OSError:
                raise PrivateStorageQuotaError() from None
            finally:
                if lock_fd is not None:
                    # Closing releases flock even after errors/cancellation.
                    os.close(lock_fd)

    @staticmethod
    def _namespace(tenant_id):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise PrivateStorageQuotaError()
        return hashlib.sha256(tenant_id.encode()).hexdigest()

    @staticmethod
    def _count(root_fd):
        total = 0
        with os.scandir(root_fd) as entries:
            names = [entry.name for entry in entries]
        for namespace in names:
            if namespace == _LOCK:
                continue
            if not _NAMESPACE.fullmatch(namespace):
                raise PrivateStorageQuotaError()
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                with os.scandir(namespace_fd) as objects:
                    names = [entry.name for entry in objects]
                for name in names:
                    if not (_OBJECT.fullmatch(name) or _PENDING.fullmatch(name)):
                        raise PrivateStorageQuotaError()
                    info = os.stat(name, dir_fd=namespace_fd, follow_symlinks=False)
                    _regular(info)  # No symlinks, hardlinks, directories or devices.
                    total += info.st_size
            finally:
                os.close(namespace_fd)
        return total

    @staticmethod
    def _same(namespace_fd, name, content):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=namespace_fd)
        except FileNotFoundError:
            return False
        try:
            before = os.fstat(fd)
            _regular(before)
            if before.st_size != len(content):
                raise PrivateStorageQuotaError()
            offset = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                if chunk != content[offset:offset + len(chunk)]:
                    raise PrivateStorageQuotaError()
                offset += len(chunk)
            after = os.fstat(fd)
            _regular(after)
            if offset != len(content) or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PrivateStorageQuotaError()
            return True
        finally:
            os.close(fd)

    def put(self, tenant_id, content, suffix):
        if suffix not in {"pdf", "png", "jpg"} or not isinstance(content, bytes) or not content or len(content) > MAX_OBJECT_BYTES:
            raise PrivateStorageQuotaError()
        namespace = self._namespace(tenant_id)
        name = hashlib.sha256(content).hexdigest() + "." + suffix
        key = namespace + "/" + name
        with self._locked() as root_fd:
            try:
                os.mkdir(namespace, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                # A deduplicated immutable object requires no temp file or new
                # disk allocation, including when the quota/free reserve is full.
                if self._same(namespace_fd, name, content):
                    return key
                total = self._count(root_fd)
                available = os.fstatvfs(root_fd)
                free_bytes = available.f_bavail * available.f_frsize
                if total + len(content) > self.max_bytes or free_bytes - len(content) < self.free_reserve_bytes:
                    raise PrivateStorageQuotaError()
                temporary = ".pending-" + uuid.uuid4().hex
                fd = None
                created = False
                try:
                    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=namespace_fd)
                    created = True
                    view = memoryview(content)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("Private object write failed")
                        view = view[written:]
                    os.fsync(fd)
                    os.close(fd)
                    fd = None
                    # Atomic immutable publication. Never overwrite any object.
                    os.link(temporary, name, src_dir_fd=namespace_fd, dst_dir_fd=namespace_fd, follow_symlinks=False)
                    os.fsync(namespace_fd)
                finally:
                    if fd is not None:
                        os.close(fd)
                    # Only remove this call's unpublished temporary link. Never
                    # remove a preexisting/source/final immutable object.
                    if created:
                        try:
                            os.unlink(temporary, dir_fd=namespace_fd)
                        except FileNotFoundError:
                            pass
                os.fsync(namespace_fd)
                os.fsync(root_fd)
                return key
            finally:
                os.close(namespace_fd)

    def read(self, tenant_id, key):
        namespace = self._namespace(tenant_id)
        if not isinstance(key, str) or key.count("/") != 1:
            raise PrivateStorageQuotaError()
        supplied, name = key.split("/")
        if supplied != namespace or not _OBJECT.fullmatch(name):
            raise PrivateStorageQuotaError()
        with self._root() as root_fd:
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=namespace_fd)
                try:
                    info = os.fstat(fd)
                    _regular(info)
                    if not 0 < info.st_size <= MAX_OBJECT_BYTES:
                        raise PrivateStorageQuotaError()
                    chunks, total = [], 0
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_OBJECT_BYTES:
                            raise PrivateStorageQuotaError()
                        chunks.append(chunk)
                    _regular(os.fstat(fd))
                    data = b"".join(chunks)
                    if hashlib.sha256(data).hexdigest() != name.split(".")[0]:
                        raise PrivateStorageQuotaError()
                    return data
                finally:
                    os.close(fd)
            finally:
                os.close(namespace_fd)
