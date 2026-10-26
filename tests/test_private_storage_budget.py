"""Synthetic shared-disk budget, immutable publication and Linux inode guards."""
import hashlib
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.private_storage_budget import (
    DEFAULT_FREE_RESERVE_BYTES, DEFAULT_MAX_BYTES, MAX_OBJECT_BYTES,
    PrivateStorageQuotaError, QuotaPrivateFileStore,
)
from services.whatsapp_forecast import ForecastNeedsCorrection


def namespace(tenant="hotel-a"):
    return hashlib.sha256(tenant.encode()).hexdigest()


def path_for(root, data=b"synthetic", suffix="pdf", tenant="hotel-a"):
    return Path(root) / namespace(tenant) / (hashlib.sha256(data).hexdigest() + "." + suffix)


def store(tmp_path, **kwargs):
    return QuotaPrivateFileStore(tmp_path / "private", free_reserve_bytes=0, **kwargs)


def test_defaults_shared_quota_counts_every_tenant_source_and_pdf(tmp_path):
    default = QuotaPrivateFileStore(tmp_path / "defaults")
    assert default.max_bytes == DEFAULT_MAX_BYTES == 512 * 1024 * 1024
    assert default.free_reserve_bytes == DEFAULT_FREE_RESERVE_BYTES == 128 * 1024 * 1024
    budget = store(tmp_path, max_bytes=12)
    a = budget.put("hotel-a", b"source", "png")
    b = budget.put("hotel-b", b"report", "pdf")
    assert budget.read("hotel-a", a) == b"source" and budget.read("hotel-b", b) == b"report"
    with pytest.raises(PrivateStorageQuotaError) as exc:
        budget.put("hotel-a", b"new", "jpg")
    assert isinstance(exc.value, ForecastNeedsCorrection)
    assert "Contact the operator" in str(exc.value) and str(tmp_path) not in str(exc.value)
    with pytest.raises(PrivateStorageQuotaError): budget.read("hotel-b", a)
    assert budget.read("hotel-a", a) == b"source"


def test_exact_dedup_when_full_never_allocates_temporary_or_checks_new_space(tmp_path, monkeypatch):
    import services.private_storage_budget as module
    budget = store(tmp_path, max_bytes=9)
    key = budget.put("hotel-a", b"synthetic", "pdf")
    original = os.open
    def guarded(path, *args, **kwargs):
        assert not str(path).startswith(".pending-"), "Idempotent object attempted a temporary allocation"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module.os, "open", guarded)
    monkeypatch.setattr(module.os, "fstatvfs", lambda *args: pytest.fail("Dedup checked free space unnecessarily"))
    assert budget.put("hotel-a", b"synthetic", "pdf") == key
    assert budget.read("hotel-a", key) == b"synthetic"


def _concurrent_write(root, value, barrier, results):
    try:
        budget = QuotaPrivateFileStore(root, max_bytes=10, free_reserve_bytes=0)
        barrier.wait(timeout=10)
        key = budget.put("hotel-a", value, "pdf")
        results.put(("saved", key))
    except PrivateStorageQuotaError:
        results.put(("quota", None))
    except BaseException as exc:
        results.put(("unexpected", type(exc).__name__))


def test_cross_process_flock_prevents_two_individually_valid_writes_exceeding_quota(tmp_path):
    context = multiprocessing.get_context("fork")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_concurrent_write, args=(tmp_path / "private", value, barrier, results)) for value in (b"first-88", b"second88")]
    for process in processes: process.start()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("Quota writer did not release its flock")
        assert process.exitcode == 0
    assert sorted(result[0] for result in outcomes) == ["quota", "saved"]
    objects = list((tmp_path / "private").glob("*/*.pdf"))
    assert len(objects) == 1 and sum(path.stat().st_size for path in objects) == 8
    results.close()


def test_free_space_reserve_is_checked_before_writing_and_never_deleted(tmp_path, monkeypatch):
    import services.private_storage_budget as module
    budget = QuotaPrivateFileStore(tmp_path / "private", max_bytes=100, free_reserve_bytes=20)
    monkeypatch.setattr(module.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=29, f_frsize=1))
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"0123456789", "pdf")
    assert not list(budget.root.glob("*/*"))
    monkeypatch.setattr(module.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=30, f_frsize=1))
    key = budget.put("hotel-a", b"0123456789", "pdf")
    monkeypatch.setattr(module.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=0, f_frsize=1))
    assert budget.put("hotel-a", b"0123456789", "pdf") == key


@pytest.mark.parametrize("kind", ["root", "ancestor", "namespace", "object", "lock", "other_object"])
def test_symlinks_are_never_followed_or_modified(tmp_path, kind):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"unchanged")
    root = tmp_path / "private"
    if kind == "root":
        root.symlink_to(external, target_is_directory=True)
        with pytest.raises(PrivateStorageQuotaError): QuotaPrivateFileStore(root)
    elif kind == "ancestor":
        root.symlink_to(external, target_is_directory=True)
        with pytest.raises(PrivateStorageQuotaError): QuotaPrivateFileStore(root / "nested")
        assert not (external / "nested").exists()
    else:
        budget = QuotaPrivateFileStore(root, free_reserve_bytes=0)
        if kind == "namespace":
            (root / namespace()).symlink_to(external, target_is_directory=True)
        elif kind == "lock":
            (root / ".quota.lock").symlink_to(sentinel)
        else:
            target = path_for(root)
            target.parent.mkdir()
            target.symlink_to(sentinel)
        with pytest.raises(PrivateStorageQuotaError):
            budget.put("hotel-a", b"different" if kind == "other_object" else b"synthetic", "pdf")
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in external.iterdir()) == ["sentinel"]


@pytest.mark.parametrize("kind", ["lock", "object"])
def test_hardlinked_lock_or_object_fails_closed(tmp_path, kind):
    budget = store(tmp_path)
    if kind == "lock":
        external = tmp_path / "external-lock"
        external.write_bytes(b"")
        os.link(external, budget.root / ".quota.lock")
    else:
        key = budget.put("hotel-a", b"synthetic", "pdf")
        os.link(budget.root / key, tmp_path / "external-object")
        with pytest.raises(PrivateStorageQuotaError): budget.read("hotel-a", key)
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"synthetic", "pdf")


def test_root_replacement_and_corrupt_existing_blob_fail_closed(tmp_path):
    budget = store(tmp_path)
    key = budget.put("hotel-a", b"synthetic", "pdf")
    (budget.root / key).write_bytes(b"corrupted")
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"synthetic", "pdf")
    with pytest.raises(PrivateStorageQuotaError): budget.read("hotel-a", key)
    budget.root.rename(tmp_path / "original")
    budget.root.mkdir()
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"synthetic", "pdf")
    assert (tmp_path / "original" / key).read_bytes() == b"corrupted"


def test_partial_write_failure_cleans_own_temp_releases_lock_preserves_old_objects(tmp_path, monkeypatch):
    import services.private_storage_budget as module
    budget = store(tmp_path)
    old = budget.put("hotel-a", b"old", "pdf")
    real_write = os.write
    count = [0]
    def fail(fd, data):
        count[0] += 1
        if count[0] == 1:
            return real_write(fd, data[:2])
        raise OSError("SENSITIVE DISK FAILURE")
    monkeypatch.setattr(module.os, "write", fail)
    with pytest.raises(PrivateStorageQuotaError) as exc: budget.put("hotel-a", b"new-data", "pdf")
    assert "SENSITIVE" not in str(exc.value)
    assert not list(budget.root.glob("*/.pending-*"))
    assert budget.read("hotel-a", old) == b"old"
    monkeypatch.setattr(module.os, "write", real_write)
    assert budget.put("hotel-a", b"new-data", "pdf")


def test_temp_name_collision_never_removes_preexisting_file(tmp_path, monkeypatch):
    import services.private_storage_budget as module
    budget = store(tmp_path)
    directory = budget.root / namespace()
    directory.mkdir()
    existing = directory / (".pending-" + "a" * 32)
    existing.write_bytes(b"preexisting")
    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"new-data", "pdf")
    assert existing.read_bytes() == b"preexisting"


def test_crash_leftover_temp_consumes_quota_and_is_not_deleted(tmp_path):
    budget = store(tmp_path, max_bytes=10)
    directory = budget.root / namespace()
    directory.mkdir()
    pending = directory / (".pending-" + "b" * 32)
    pending.write_bytes(b"12345678")
    with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", b"new", "pdf")
    assert pending.read_bytes() == b"12345678"


def test_invalid_budget_and_object_bounds(tmp_path):
    for args in [{"max_bytes": 0}, {"max_bytes": True}, {"free_reserve_bytes": -1}, {"free_reserve_bytes": False}]:
        with pytest.raises(ValueError): QuotaPrivateFileStore(tmp_path / "private", **args)
    budget = store(tmp_path)
    for data, suffix in [(b"", "pdf"), (b"small", "exe"), (b"x" * (MAX_OBJECT_BYTES + 1), "pdf")]:
        with pytest.raises(PrivateStorageQuotaError): budget.put("hotel-a", data, suffix)
