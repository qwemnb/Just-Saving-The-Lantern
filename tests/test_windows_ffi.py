from __future__ import annotations

import ctypes
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import reset_database, windows_native
from app.maintenance_lock import (
    ResetRecoveryRequiredError,
    acquire_database_lease,
)
from app.read_snapshot import run_read_snapshot


@unittest.skipUnless(os.name == "nt", "Windows native FFI tests")
class WindowsNativeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = {
            "alpha.txt": b"alpha\n",
            "beta.txt": b"beta\n",
            "gamma.txt": b"gamma\n",
        }
        for name, content in self.files.items():
            (self.root / name).write_bytes(content)

    def logical_snapshot(self) -> tuple[tuple[object, ...], ...]:
        rows: list[tuple[object, ...]] = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            info = path.stat()
            rows.append(
                (
                    path.name,
                    path.is_file(),
                    info.st_size,
                    info.st_mtime_ns,
                    path.read_bytes() if path.is_file() else None,
                )
            )
        return tuple(rows)

    def test_nt_query_directory_file_has_one_stable_canonical_prototype(
        self,
    ) -> None:
        query = windows_native.nt_query_directory_file()
        argtypes_object = query.argtypes
        io_status_type = windows_native.IO_STATUS_BLOCK
        self.assertIs(query.argtypes[4]._type_, io_status_type)
        self.assertIs(query.argtypes[9], ctypes.c_void_p)

        self.assertEqual(
            set(reset_database._windows_enumerate_directory_direct(self.root)),
            set(self.files),
        )
        self.assertIs(type(windows_native.IO_STATUS_BLOCK()), io_status_type)
        handle = reset_database._windows_open_directory_chain(self.root)
        try:
            self.assertIsNotNone(
                reset_database._windows_relative_entry_identity(
                    handle, "alpha.txt"
                )
            )
            self.assertIsNone(
                reset_database._windows_relative_entry_identity(
                    handle, "missing.txt"
                )
            )
        finally:
            reset_database._windows_close_handle(handle)
        self.assertEqual(
            set(reset_database._windows_enumerate_directory_direct(self.root)),
            set(self.files),
        )

        self.assertIs(windows_native.nt_query_directory_file(), query)
        self.assertIs(query.argtypes, argtypes_object)
        self.assertIs(query.argtypes[4]._type_, io_status_type)
        self.assertIs(query.argtypes[9], ctypes.c_void_p)

    def test_binding_initialization_is_once_under_concurrency(self) -> None:
        class CountingFunction:
            def __init__(self) -> None:
                object.__setattr__(self, "argtypes", None)
                object.__setattr__(self, "restype", None)
                object.__setattr__(self, "argtype_sets", 0)
                object.__setattr__(self, "restype_sets", 0)

            def __setattr__(self, name: str, value: object) -> None:
                if name == "argtypes":
                    object.__setattr__(
                        self, "argtype_sets", self.argtype_sets + 1
                    )
                elif name == "restype":
                    object.__setattr__(
                        self, "restype_sets", self.restype_sets + 1
                    )
                object.__setattr__(self, name, value)

        function = CountingFunction()
        barrier = threading.Barrier(12)

        def resolve() -> object:
            barrier.wait()
            return windows_native.nt_query_directory_file()

        with patch.object(
            ctypes.windll.ntdll, "NtQueryDirectoryFile", function
        ), ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _index: resolve(), range(12)))

        self.assertTrue(all(result is function for result in results))
        self.assertEqual(function.argtype_sets, 1)
        self.assertEqual(function.restype_sets, 1)

    def test_every_shared_windows_binding_keeps_its_configured_prototype(
        self,
    ) -> None:
        getters = (
            windows_native.get_file_information_by_handle_ex,
            windows_native.create_file,
            windows_native.close_handle,
            windows_native.get_last_error,
            windows_native.flush_file_buffers,
            windows_native.set_file_information_by_handle,
            windows_native.lock_file_ex,
            windows_native.unlock_file_ex,
            windows_native.nt_create_file,
            windows_native.nt_query_directory_file,
            windows_native.nt_query_information_file,
            windows_native.nt_set_information_file,
        )
        for getter in getters:
            with self.subTest(binding=getter.__name__):
                function = getter()
                argtypes_object = function.argtypes
                restype = function.restype
                self.assertIs(getter(), function)
                self.assertIs(function.argtypes, argtypes_object)
                self.assertIs(function.restype, restype)

        self.assertIs(
            windows_native.nt_create_file().argtypes[2]._type_,
            windows_native.OBJECT_ATTRIBUTES,
        )
        self.assertIs(
            windows_native.nt_create_file().argtypes[3]._type_,
            windows_native.IO_STATUS_BLOCK,
        )
        self.assertIs(
            windows_native.nt_query_information_file().argtypes[1]._type_,
            windows_native.IO_STATUS_BLOCK,
        )
        self.assertIs(
            windows_native.nt_set_information_file().argtypes[1]._type_,
            windows_native.IO_STATUS_BLOCK,
        )
        self.assertIs(
            windows_native.lock_file_ex().argtypes[5]._type_,
            windows_native.OVERLAPPED,
        )
        self.assertIs(
            windows_native.unlock_file_ex().argtypes[4]._type_,
            windows_native.OVERLAPPED,
        )

    def test_same_helper_concurrency_is_stable_and_read_only(self) -> None:
        before = self.logical_snapshot()
        expected = set(self.files)
        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def enumerate_repeatedly(_worker: int) -> None:
            barrier.wait()
            for _iteration in range(25):
                names = reset_database._windows_enumerate_directory_direct(
                    self.root
                )
                self.assertEqual(set(names), expected)
                self.assertEqual(len(names), len(expected))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(enumerate_repeatedly, range(worker_count)))

        self.assertEqual(self.logical_snapshot(), before)

    def test_mixed_helper_concurrency_is_stable_and_read_only(self) -> None:
        before = self.logical_snapshot()
        expected_names = set(self.files)
        seed_handle = reset_database._windows_open_directory_chain(self.root)
        try:
            expected_identities = {
                name: reset_database._windows_relative_entry_identity(
                    seed_handle, name
                )
                for name in self.files
            }
        finally:
            reset_database._windows_close_handle(seed_handle)

        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def mixed(worker: int) -> None:
            barrier.wait()
            for _iteration in range(20):
                handle = reset_database._windows_open_directory_chain(self.root)
                try:
                    if worker % 2 == 0:
                        names = reset_database._windows_enumerate_directory_handle(
                            handle
                        )
                        self.assertEqual(set(names), expected_names)
                        self.assertEqual(len(names), len(expected_names))
                    else:
                        for name, identity in expected_identities.items():
                            self.assertEqual(
                                reset_database._windows_relative_entry_identity(
                                    handle, name
                                ),
                                identity,
                            )
                        self.assertIsNone(
                            reset_database._windows_relative_entry_identity(
                                handle, "missing.txt"
                            )
                        )
                finally:
                    reset_database._windows_close_handle(handle)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(mixed, range(worker_count)))

        self.assertEqual(self.logical_snapshot(), before)

    def test_concurrent_read_snapshots_cover_maintenance_lock_path(self) -> None:
        database = self.root / "fixture.db"
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                "wal",
            )
            connection.execute(
                "CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO values_table(value) VALUES (?)",
                [("one",), ("two",), ("three",)],
            )
            connection.commit()
        finally:
            connection.close()

        lease = acquire_database_lease(database, shared=True)
        lease.close()
        database_before = database.read_bytes()
        directory_before = self.logical_snapshot()
        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def read_repeatedly(worker: int) -> None:
            barrier.wait()
            for _iteration in range(12):
                if worker % 2:
                    operation = lambda db: db.execute(
                        "SELECT COUNT(*) FROM values_table"
                    ).fetchone()[0]
                    expected: object = 3
                else:
                    operation = lambda db: [
                        row[0]
                        for row in db.execute(
                            "SELECT value FROM values_table ORDER BY id"
                        )
                    ]
                    expected = ["one", "two", "three"]
                result = run_read_snapshot(database, operation)
                self.assertEqual(result.value, expected)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(read_repeatedly, range(worker_count)))

        self.assertEqual(database.read_bytes(), database_before)
        self.assertEqual(self.logical_snapshot(), directory_before)
        self.assertFalse(Path(f"{database}-wal").exists())
        self.assertFalse(Path(f"{database}-shm").exists())

    def test_reset_state_and_native_failure_still_fail_closed(self) -> None:
        database = self.root / "blocked.db"
        database.write_bytes(b"not opened")
        state = self.root / ".helios-room-reset-state.synthetic.json"
        state.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ResetRecoveryRequiredError):
            acquire_database_lease(database, shared=True)
        self.assertFalse((self.root / ".helios-room-database.lock").exists())

        state.unlink()
        with patch(
            "app.reset_database.windows_native.nt_query_directory_file",
            side_effect=windows_native.WindowsNativeBindingError("synthetic"),
        ):
            with self.assertRaises(ResetRecoveryRequiredError):
                acquire_database_lease(database, shared=True)
        self.assertFalse((self.root / ".helios-room-database.lock").exists())

        lease = acquire_database_lease(database, shared=True)
        lease.close()


if __name__ == "__main__":
    unittest.main()
