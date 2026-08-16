from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

import app.reset_database as reset_module
import app.reset_protocol_v2 as reset_v2
import app.maintenance_lock as maintenance_lock

from app.reset_database import (
    IGNORE_BLOCK,
    DatabaseResetError,
    execute_database_reset,
    plan_database_reset,
    recover_database_reset,
)
from app.database import (
    DatabaseInitializationError,
    connect_database,
    initialize_database,
)
from app.maintenance_lock import ResetRecoveryRequiredError
from app.migration import DatabaseMigrationError, migrate_database
from app.preflight import DatabasePreflightError, preflight_database
from app.schema_validation import validate_v14_foundation


class _FakeWin32Function:
    def __init__(self, result: object) -> None:
        self.result = result
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *arguments: object) -> object:
        self.calls.append(arguments)
        return self.result


class ResetProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / ".gitignore").write_text(IGNORE_BLOCK, encoding="utf-8")
        (self.root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        self.git("init")
        self.git("config", "user.email", "reset@example.invalid")
        self.git("config", "user.name", "Reset Test")
        self.git("add", ".gitignore", "tracked.txt")
        self.git("commit", "-m", "fixture")
        self.database = self.root / "data" / "helios.db"
        self.make_v12()
        self.real_probe_durability = reset_v2._probe_durability
        self.real_volume_flush = reset_v2._open_and_flush_volume
        self.durability_patch = patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=lambda root: self.synthetic_durability(Path(root)),
        )
        self.flush_patch = patch("app.reset_protocol_v2._flush_namespace")
        self.volume_patch = patch("app.reset_protocol_v2._open_and_flush_volume")
        self.directory_probe_patch = patch(
            "app.reset_protocol_v2._directory_probe",
            side_effect=lambda name, _path, identity: {
                "error_code": None,
                "flush_succeeded": True,
                "identity": identity,
                "name": name,
                "state": "present",
            },
        )
        self.stable_authority_patch = patch(
            "app.reset_protocol_v2._inspect_stable_durability_authority",
            side_effect=lambda root: self.synthetic_stable_authority(Path(root)),
        )
        self.durability_patch.start()
        self.flush_patch.start()
        self.volume_patch.start()
        self.directory_probe_patch.start()
        self.stable_authority_patch.start()
        self.addCleanup(self.durability_patch.stop)
        self.addCleanup(self.flush_patch.stop)
        self.addCleanup(self.volume_patch.stop)
        self.addCleanup(self.directory_probe_patch.stop)
        self.addCleanup(self.stable_authority_patch.stop)

    def synthetic_durability(self, root: Path) -> dict[str, object]:
        data_identity = reset_module._path_identity(root / "data")
        repository_identity = reset_module._path_identity(root)
        serial = repository_identity.split(":")[1]
        backups = root / "backups"
        backup_identity = reset_module._path_identity(backups) if backups.exists() else None
        probes = [
            {"error_code": None, "flush_succeeded": True, "identity": repository_identity, "name": "repository", "state": "present"},
            {"error_code": None, "flush_succeeded": True, "identity": data_identity, "name": "data", "state": "present"},
            ({"error_code": None, "flush_succeeded": True, "identity": backup_identity, "name": "backups", "state": "present"}
             if backup_identity is not None else
             {"error_code": None, "flush_succeeded": None, "identity": None, "name": "backups", "state": "absent"}),
        ]
        return {
            "api_contract": "win32_flush_v1",
            "directory_probes": probes,
            "drive_type": 3,
            "fallback_reason": None if backup_identity is not None else "backups_absent",
            "filesystem_name": "NTFS",
            "handle_volume_serial_hex": serial,
            "mode": "directory_flush" if backup_identity is not None else "volume_flush",
            "volume_flush_succeeded": None if backup_identity is not None else True,
            "volume_guid": "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
            "volume_information_serial_hex": "00000001",
            "volume_opened": backup_identity is None,
            "volume_root_resolved": True,
        }

    def synthetic_stable_authority(self, root: Path) -> dict[str, object]:
        durability = self.synthetic_durability(root)
        probes = {
            item["name"]: item for item in durability["directory_probes"]
        }
        return {
            "drive_type": durability["drive_type"],
            "filesystem_name": durability["filesystem_name"],
            "handle_volume_serial_hex": durability["handle_volume_serial_hex"],
            "identities": {
                name: probes[name]["identity"]
                for name in ("repository", "data", "backups")
            },
            "volume_guid": durability["volume_guid"],
            "volume_information_serial_hex": durability["volume_information_serial_hex"],
            "volume_root_resolved": True,
        }

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return result.stdout

    def make_v12(self) -> None:
        self.make_v12_at(self.database)

    @staticmethod
    def make_v12_at(database: Path) -> None:
        schema = Path(__file__).parents[1] / "schema" / "helios_room_schema_v1_2.sql"
        connection = sqlite3.connect(database)
        connection.executescript(schema.read_text(encoding="utf-8"))
        room = connection.execute(
            "INSERT INTO rooms(room_key,name) VALUES ('main','The Room')"
        ).lastrowid
        peter = connection.execute(
            "INSERT INTO participants(participant_key,name,participant_type) VALUES ('peter','Peter','human')"
        ).lastrowid
        helios = connection.execute(
            "INSERT INTO participants(participant_key,name,participant_type) VALUES ('helios','Helios','ai')"
        ).lastrowid
        connection.executemany(
            "INSERT INTO room_participants(room_id,participant_id) VALUES (?,?)",
            ((room, peter), (room, helios)),
        )
        connection.execute(
            """INSERT INTO participant_configs
               (participant_id,provider,config_label,settings_json,tools_json)
               VALUES (?,'openai','initial','{}','[]')""", (helios,)
        )
        connection.commit()
        connection.close()

    def make_isolated_fixture(self, name: str) -> tuple[Path, Path]:
        root = self.root / name
        (root / "data").mkdir(parents=True)
        (root / ".gitignore").write_text(IGNORE_BLOCK, encoding="utf-8")
        (root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        for arguments in (
            ("init",),
            ("config", "user.email", "reset@example.invalid"),
            ("config", "user.name", "Reset Test"),
            ("add", ".gitignore", "tracked.txt"),
            ("commit", "-m", "fixture"),
        ):
            subprocess.run(
                ["git", "-C", str(root), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        database = root / "data" / "helios.db"
        self.make_v12_at(database)
        return root, database

    def status(self) -> str:
        return self.git(
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        )

    def make_legacy_ready_journal(self, token: str, backup_relative: str, audit_relative: str) -> Path:
        backup = self.root / backup_relative
        backup.parent.mkdir(exist_ok=True)
        source = reset_module._open_reset_source(self.database)
        try:
            source_label, digest = reset_module._validate_source(source)
            target = sqlite3.connect(backup)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.rollback()
            source.close()
        observations = reset_module._observations(self.database)
        active = {
            "database": self.database,
            "wal": Path(f"{self.database}-wal"),
            "shm": Path(f"{self.database}-shm"),
        }
        components = {
            key: (reset_module._component(path, f"data/{path.name}") if path.exists() else None)
            for key, path in active.items()
        }
        source_files = {
            key: None if value is None else reset_module._source_file(value)
            for key, value in components.items()
        }
        quarantine = {
            key: None if value is None else {
                "identity": value["identity"],
                "path": f"data/.helios.db{suffix}.reset-{token}.original",
            }
            for key, suffix, value in (
                ("database", "", source_files["database"]),
                ("wal", "-wal", source_files["wal"]),
                ("shm", "-shm", source_files["shm"]),
            )
        }
        journal = {
            "audit_path": audit_relative,
            "backup": {"identity": reset_module._path_identity(backup), "path": backup_relative, "sha256": reset_module._stable_sha256(backup)},
            "database_path": "data/helios.db",
            "implementation_commit": "d9d9717b4e882c19ef41e44a6f0c63fc062de41a",
            "journal_sequence": 1,
            "journal_version": 1,
            "logical_digest": digest,
            "plan_manifest_sha256": token,
            "plan_token": token,
            "quarantine": quarantine,
            "recovery_commit": reset_module.RECOVERY_COMMIT_BY_SCHEMA[source_label],
            "reset_protocol_version": "room_shared_reset_v1",
            "source_files": source_files,
            "source_observations": {"before_open": observations, "after_close": observations},
            "source_schema_label": source_label,
            "stage": "ready_to_quarantine",
            "updated_at": "2026-08-14T12:00:00.000Z",
        }
        path = self.root / "data" / ".helios-room-reset-state.json"
        path.write_bytes(reset_v2._canonical_bytes(journal))
        return path

    def test_filesystem_only_plan_is_closed_stable_and_nonmutating(self) -> None:
        before_bytes = self.database.read_bytes()
        before_status = self.status()
        with patch(
            "app.reset_database.sqlite3.connect",
            side_effect=AssertionError("planning must not open SQLite"),
        ):
            plan = plan_database_reset(
                "data/helios.db", repository_root=self.root
            )
        self.assertEqual(set(plan), {
            "audit_path", "backup_path", "database_identity", "database_path",
            "database_sha256", "implementation_commit", "plan_token",
            "journal_storage_protocol_version", "recovery_commit_by_schema",
            "reset_protocol_version", "reviewed_plan_manifest", "status",
        })
        self.assertEqual(plan["database_path"], "data/helios.db")
        self.assertEqual(plan["status"], "planned")
        self.assertRegex(plan["plan_token"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.database.read_bytes(), before_bytes)
        self.assertEqual(self.status(), before_status)

    def test_windows_platform_gate_precedes_repository_lock_and_sqlite(self) -> None:
        calls = (
            "app.reset_protocol_v2._git_evidence",
            "app.reset_protocol_v2._probe_durability",
            "app.reset_protocol_v2.acquire_database_lease",
            "app.reset_database.sqlite3.connect",
        )
        for mode in ("plan", "execute", "recover"):
            with self.subTest(mode=mode), patch.object(reset_v2.os, "name", "posix"):
                guards = [
                    patch(name, side_effect=AssertionError(f"{name} must remain uncalled"))
                    for name in calls
                ]
                started = [guard.start() for guard in guards]
                self.addCleanup(lambda values=guards: [value.stop() for value in values])
                with self.assertRaises(DatabaseResetError) as unsupported:
                    if mode == "plan":
                        plan_database_reset("data/helios.db", repository_root=self.root)
                    elif mode == "execute":
                        execute_database_reset(
                            "data/helios.db",
                            expected_plan_token="a" * 64,
                            expected_backup_path="backups/helios-pre-room-shared-reset-20260814T120000000Z.db",
                            expected_audit_path="backups/helios-pre-room-shared-reset-20260814T120000000Z.audit.json",
                            confirm_destroy_canonical_history=True,
                            repository_root=self.root,
                        )
                    else:
                        recover_database_reset(
                            "data/helios.db", expected_plan_token="a" * 64,
                            action="restore-source", confirm_reset_recovery=True,
                            repository_root=self.root,
                        )
                self.assertEqual(unsupported.exception.code, "reset_platform_unsupported")
                self.assertTrue(all(item.call_count == 0 for item in started))
                for guard in reversed(guards):
                    guard.stop()

    def test_plan_manifest_rejects_noncanonical_and_mutated_closed_fields(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        self.assertEqual(len(plan["reviewed_plan_manifest"]["artifact_paths"]), 25)
        self.assertEqual(len(plan["reviewed_plan_manifest"]["path_absence"]), 19)
        self.assertEqual(
            reset_v2._sha256(reset_v2._canonical_bytes(plan["reviewed_plan_manifest"])),
            plan["plan_token"],
        )

        mutations = []
        extra = json.loads(json.dumps(plan))
        extra["reviewed_plan_manifest"]["future"] = True
        mutations.append(extra)
        unstable = json.loads(json.dumps(plan))
        unstable["reviewed_plan_manifest"]["filesystem"]["database"]["sha256_status"] = "unstable"
        unstable["reviewed_plan_manifest"]["filesystem"]["database"]["sha256"] = None
        mutations.append(unstable)
        mapping = json.loads(json.dumps(plan))
        mapping["reviewed_plan_manifest"]["recovery_commit_by_schema"]["1.2"] = "f" * 40
        mapping["recovery_commit_by_schema"] = dict(mapping["reviewed_plan_manifest"]["recovery_commit_by_schema"])
        mutations.append(mapping)
        uppercase_guid = json.loads(json.dumps(plan))
        uppercase_guid["reviewed_plan_manifest"]["durability"]["volume_guid"] = (
            "\\\\?\\Volume{AAAAAAAA-0000-0000-0000-000000000001}\\"
        )
        mutations.append(uppercase_guid)
        boolean_integer = json.loads(json.dumps(plan))
        boolean_integer["reviewed_plan_manifest"]["generation_version"] = True
        mutations.append(boolean_integer)
        for value in mutations:
            value["plan_token"] = reset_v2._sha256(
                reset_v2._canonical_bytes(value["reviewed_plan_manifest"])
            )
            with self.assertRaises(DatabaseResetError) as invalid:
                reset_v2._validate_plan(value)
            self.assertEqual(invalid.exception.code, "reset_recovery_invalid")

        for raw in (
            b"\xef\xbb\xbf{}\n",
            b'{"a":1,"a":1}\n',
            b'{ "a":1}\n',
        ):
            with self.assertRaises(DatabaseResetError):
                reset_v2._load_canonical(raw)

    def test_reviewed_plan_path_rejects_nonlocal_and_ambiguous_forms_before_open(self) -> None:
        invalid_paths = (
            "reviewed.json",
            "C:reviewed.json",
            "C:\\folder\\..\\reviewed.json",
            "C:\\folder\\.\\reviewed.json",
            "C:\\folder\\reviewed.json:stream",
            "C:\\folder\\reviewed.json.",
            "C:\\folder \\reviewed.json",
            "\\\\server\\share\\reviewed.json",
            "\\\\?\\C:\\reviewed.json",
            "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\reviewed.json",
            "C:/folder/reviewed.json",
        )
        with patch(
            "app.reset_protocol_v2._open_reviewed_plan_handle",
            side_effect=AssertionError("invalid path must not be opened"),
        ) as opener:
            for path in invalid_paths:
                with self.subTest(path=path), self.assertRaises(DatabaseResetError) as invalid:
                    reset_v2._read_reviewed_plan(path, "a" * 64, "b" * 40)
                self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
        opener.assert_not_called()

    def test_reviewed_plan_rejects_hard_link_without_path_reopen(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        reviewed = self.root / "reviewed-plan.json"
        linked = self.root / "linked-plan.json"
        reviewed.write_bytes(reset_v2._canonical_bytes(plan))
        os.link(reviewed, linked)
        try:
            with self.assertRaises(DatabaseResetError) as invalid:
                reset_v2._read_reviewed_plan(
                    str(linked), plan["plan_token"], plan["implementation_commit"]
                )
            self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
            self.assertEqual(reviewed.read_bytes(), linked.read_bytes())
        finally:
            linked.unlink()
            reviewed.unlink()

    def test_runtime_interlock_rechecks_after_lock_acquisition(self) -> None:
        state = self.root / "data" / ".helios-room-reset-state.hostile"
        original_lock = maintenance_lock._lock

        def create_after_lock(lease: object) -> None:
            original_lock(lease)
            state.write_bytes(b"synthetic")

        with patch("app.maintenance_lock._lock", side_effect=create_after_lock), patch(
            "app.database.sqlite3.connect",
            side_effect=AssertionError("SQLite must remain unopened"),
        ) as sqlite_open:
            with self.assertRaises(ResetRecoveryRequiredError):
                connect_database(self.database)
        sqlite_open.assert_not_called()
        self.assertEqual(state.read_bytes(), b"synthetic")
        state.unlink()

    def test_legacy_journal_converts_through_evidence_record_without_native_manifest(self) -> None:
        token = "a" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000000Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000000Z.audit.json"
        legacy_path = self.make_legacy_ready_journal(token, backup, audit)
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=token,
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertEqual(report["reset_protocol_version"], "room_shared_reset_v1")
        self.assertNotIn("journal_storage_protocol_version", report)
        self.assertFalse(legacy_path.exists())
        self.assertFalse(any(
            path.name.startswith(".helios-room-reset-state.")
            for path in (self.root / "data").iterdir()
        ))
        self.assertTrue(Path(f"{self.root / audit}.generation-chain.json").is_file())

    def test_interrupted_legacy_evidence_partial_recovers_with_changed_transient_probe(self) -> None:
        token = "b" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000001Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000001Z.audit.json"
        self.make_legacy_ready_journal(token, backup, audit)
        with self.assertRaises(KeyboardInterrupt):
            recover_database_reset(
                "data/helios.db", expected_plan_token=token,
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
                crash_checkpoint=lambda checkpoint: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if checkpoint == "legacy_evidence:partial_write" else None
                ),
            )
        old_partial = next(
            path for path in (self.root / "data").iterdir()
            if ".legacy-evidence." in path.name and path.name.endswith(".partial")
        )
        changed = self.synthetic_durability(self.root)
        changed["mode"] = "volume_flush"
        changed["fallback_reason"] = "directory_flush_error"
        changed["volume_opened"] = True
        changed["volume_flush_succeeded"] = True
        changed["directory_probes"][0] = {
            **changed["directory_probes"][0],
            "flush_succeeded": False,
            "error_code": 5,
        }
        with patch("app.reset_protocol_v2._probe_durability", return_value=changed):
            report = recover_database_reset(
                "data/helios.db", expected_plan_token=token,
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
            )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(old_partial.exists())

    def test_evidence_partial_post_disposition_failure_stops_before_regeneration(self) -> None:
        token = "c" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000002Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000002Z.audit.json"
        legacy_path = self.make_legacy_ready_journal(token, backup, audit)
        with self.assertRaises(KeyboardInterrupt):
            recover_database_reset(
                "data/helios.db", expected_plan_token=token,
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
                crash_checkpoint=lambda checkpoint: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if checkpoint == "legacy_evidence:partial_write" else None
                ),
            )
        with patch(
            "app.reset_protocol_v2._flush_namespace",
            side_effect=DatabaseResetError("reset_failed", "synthetic"),
        ):
            with self.assertRaises(DatabaseResetError) as failed:
                recover_database_reset(
                    "data/helios.db", expected_plan_token=token,
                    action="restore-source", confirm_reset_recovery=True,
                    repository_root=self.root,
                )
        self.assertEqual(failed.exception.code, "reset_failed")
        self.assertTrue(legacy_path.is_file())
        self.assertFalse(any(
            ".legacy-evidence." in path.name for path in (self.root / "data").iterdir()
        ))

    def test_evidence_partial_probe_failure_is_premutation_and_sanitized(self) -> None:
        token = "e" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000004Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000004Z.audit.json"
        legacy_path = self.make_legacy_ready_journal(token, backup, audit)
        with self.assertRaises(KeyboardInterrupt):
            recover_database_reset(
                "data/helios.db", expected_plan_token=token,
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
                crash_checkpoint=lambda checkpoint: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if checkpoint == "legacy_evidence:partial_write" else None
                ),
            )
        partial = next(
            path for path in (self.root / "data").iterdir()
            if ".legacy-evidence." in path.name and path.name.endswith(".partial")
        )
        partial_before = partial.read_bytes()
        legacy_before = legacy_path.read_bytes()
        preliminary = self.synthetic_durability(self.root)
        unsupported = DatabaseResetError(
            "reset_durability_unsupported", "synthetic capability failure"
        )
        with patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=[preliminary, unsupported],
        ):
            with self.assertRaises(DatabaseResetError) as failed:
                recover_database_reset(
                    "data/helios.db", expected_plan_token=token,
                    action="restore-source", confirm_reset_recovery=True,
                    repository_root=self.root,
                )
        self.assertEqual(failed.exception.code, "reset_durability_unsupported")
        self.assertEqual(partial.read_bytes(), partial_before)
        self.assertEqual(legacy_path.read_bytes(), legacy_before)
        self.assertFalse(any(
            reset_v2.GENERATION_RE.fullmatch(path.name)
            for path in (self.root / "data").iterdir()
        ))

    def test_complete_evidence_partial_is_promoted_with_exact_nine_key_record(self) -> None:
        token = "f" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000005Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000005Z.audit.json"
        self.make_legacy_ready_journal(token, backup, audit)
        original_promote = reset_v2._promote_open_control

        def stop_evidence_install(
            control: object, final: Path, expected_raw: bytes | None, **kwargs: object
        ) -> str:
            if ".legacy-evidence." in final.name:
                raise KeyboardInterrupt()
            return original_promote(control, final, expected_raw, **kwargs)

        with patch(
            "app.reset_protocol_v2._promote_open_control",
            side_effect=stop_evidence_install,
        ):
            with self.assertRaises(KeyboardInterrupt):
                recover_database_reset(
                    "data/helios.db", expected_plan_token=token,
                    action="restore-source", confirm_reset_recovery=True,
                    repository_root=self.root,
                )
        partial = next(
            path for path in (self.root / "data").iterdir()
            if ".legacy-evidence." in path.name and path.name.endswith(".partial")
        )
        record = json.loads(partial.read_text(encoding="utf-8"))
        self.assertEqual(set(record), reset_v2.EVIDENCE_RECORD_KEYS)
        self.assertEqual(reset_v2._sha256(partial.read_bytes()), partial.name.split(".")[-3])
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=token,
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(partial.exists())

    def test_json_control_partial_uses_one_handle_through_promotion(self) -> None:
        partial = self.root / "data" / ".control.json.partial"
        final = self.root / "data" / ".control.json"
        authority = self.synthetic_durability(self.root)
        real_flush = reset_module._windows_flush_file_handle
        real_rename = reset_module._windows_rename
        flushed: list[int] = []
        renamed: list[int] = []

        def record_flush(handle: int) -> None:
            flushed.append(handle)
            real_flush(handle)

        def record_rename(
            handle: int, target: Path, parent: int, *, replace: bool
        ) -> None:
            renamed.append(handle)
            real_rename(handle, target, parent, replace=replace)

        with patch(
            "app.reset_database._windows_flush_file_handle",
            side_effect=record_flush,
        ), patch(
            "app.reset_database._windows_rename",
            side_effect=record_rename,
        ):
            raw, identity = reset_v2._write_and_install_json(
                partial, final, {"kind": "synthetic"}, authority
            )
        self.assertEqual(final.read_bytes(), raw)
        self.assertEqual(reset_module._path_identity(final), identity)
        self.assertFalse(partial.exists())
        self.assertEqual(len(renamed), 1)
        self.assertGreaterEqual(len(flushed), 2)
        self.assertTrue(all(handle == renamed[0] for handle in flushed))

    def test_sqlite_staging_elevates_via_same_parent_and_same_object(self) -> None:
        staging = self.root / "data" / ".sqlite-staging"
        final = self.root / "data" / ".sqlite-installed"
        real_create = reset_module._windows_create_relative
        real_flush = reset_module._windows_flush_file_handle
        real_rename = reset_module._windows_rename
        creates: list[tuple[bool, int | None, int, int]] = []
        mutation_events: list[tuple[str, int]] = []

        def record_create(
            parent: int,
            name: str,
            *,
            directory: bool,
            create_new: bool = True,
            desired_access: int | None = None,
            share_access: int = 0x1 | 0x2 | 0x4,
        ) -> int:
            handle = real_create(
                parent,
                name,
                directory=directory,
                create_new=create_new,
                desired_access=desired_access,
                share_access=share_access,
            )
            if name == staging.name:
                creates.append((create_new, desired_access, share_access, handle))
            return handle

        def record_flush(handle: int) -> None:
            mutation_events.append(("flush", handle))
            real_flush(handle)

        def record_rename(
            handle: int, target: Path, parent: int, *, replace: bool
        ) -> None:
            mutation_events.append(("rename", handle))
            real_rename(handle, target, parent, replace=replace)

        with patch(
            "app.reset_database._windows_create_relative",
            side_effect=record_create,
        ), patch(
            "app.reset_database._windows_flush_file_handle",
            side_effect=record_flush,
        ), patch(
            "app.reset_database._windows_rename",
            side_effect=record_rename,
        ), reset_v2._open_sqlite_staging(staging) as state:
            original_identity = state.identity
            connection = sqlite3.connect(staging)
            try:
                connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
                connection.execute("INSERT INTO fixture VALUES ('safe')")
                connection.commit()
            finally:
                connection.close()
            control = reset_v2._elevate_sqlite_staging(state)
            self.assertEqual(control.identity, original_identity)
            reset_v2._promote_open_control(control, final, expected_raw=None)
        self.assertEqual(len(creates), 2)
        initial, elevated = creates
        self.assertTrue(initial[0])
        self.assertEqual(initial[1], 0x00100000 | 0x00000080)
        self.assertEqual(initial[2], 0x1 | 0x2 | 0x4)
        self.assertFalse(elevated[0])
        self.assertIsNotNone(elevated[1])
        self.assertTrue(int(elevated[1]) & 0x00010000)
        self.assertEqual(elevated[2], 0)
        self.assertNotEqual(initial[3], elevated[3])
        rename_index = next(
            index for index, event in enumerate(mutation_events)
            if event[0] == "rename"
        )
        self.assertGreater(rename_index, 0)
        self.assertEqual(mutation_events[rename_index - 1][0], "flush")
        self.assertEqual(
            mutation_events[rename_index - 1][1],
            mutation_events[rename_index][1],
        )
        connection = sqlite3.connect(final)
        try:
            self.assertEqual(
                connection.execute("SELECT value FROM fixture").fetchone()[0],
                "safe",
            )
        finally:
            connection.close()

    def test_legacy_next_accepts_only_exact_incomplete_successor_prefix(self) -> None:
        token = "1" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000006Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000006Z.audit.json"
        state = self.make_legacy_ready_journal(token, backup, audit)
        journal = json.loads(state.read_text(encoding="utf-8"))
        successor = {
            **journal,
            "journal_sequence": journal["journal_sequence"] + 1,
            "stage": "quarantining",
            "updated_at": "2026-08-14T12:00:00.001Z",
        }
        successor_raw = reset_v2._canonical_bytes(successor)
        next_path = Path(f"{state}.next")
        next_path.write_bytes(successor_raw[:-7])
        selection = reset_v2._select_legacy(self.root, token)
        self.assertEqual(selection.selected_path, state)

        invalid_candidates = (
            reset_v2._canonical_bytes({
                **successor,
                "journal_sequence": journal["journal_sequence"] + 2,
            }),
            b'{"complete":"but hostile"}\n',
            successor_raw[:-7] + b"x",
        )
        for candidate in invalid_candidates:
            with self.subTest(candidate=candidate[-24:]):
                next_path.write_bytes(candidate)
                with self.assertRaises(DatabaseResetError) as invalid:
                    reset_v2._select_legacy(self.root, token)
                self.assertEqual(invalid.exception.code, "reset_recovery_invalid")

    def test_incomplete_native_shm_allows_only_sqlite_managed_changes(self) -> None:
        planned = {
            "exists": True,
            "file_type": "regular",
            "identity": "windows:0000000000000001:" + "2" * 32,
            "link_count": 1,
            "mtime_ns": 10,
            "sha256": "3" * 64,
            "size": 32,
        }
        changed = {
            **planned,
            "mtime_ns": 20,
            "sha256": "4" * 64,
            "size": 64,
        }
        self.assertTrue(reset_v2._matches_incomplete_native_observation(
            "database_shm", changed, planned
        ))
        self.assertFalse(reset_v2._matches_incomplete_native_observation(
            "database", changed, planned
        ))
        for mutation in (
            {"exists": False},
            {"file_type": "symlink"},
            {"identity": "windows:0000000000000001:" + "5" * 32},
            {"link_count": 2},
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(
                    reset_v2._matches_incomplete_native_observation(
                        "database_shm", {**changed, **mutation}, planned
                    )
                )

    def test_close_handle_uses_pointer_safe_ctypes_contract(self) -> None:
        class FakeClose:
            argtypes: object = None
            restype: object = None

            def __init__(self) -> None:
                self.values: list[int] = []

            def __call__(self, handle: object) -> int:
                self.values.append(int(handle.value))
                return 1

        fake = FakeClose()
        high_handle = 0xFEDCBA987654321
        with patch.object(ctypes.windll.kernel32, "CloseHandle", fake):
            reset_module._windows_close_handle(high_handle)
        self.assertEqual(fake.argtypes, [wintypes.HANDLE])
        self.assertIs(fake.restype, wintypes.BOOL)
        self.assertEqual(fake.values, [high_handle])

    def test_volume_flush_preserves_full_width_handle_and_exact_ffi(self) -> None:
        high_handle = 0x1234567887654321
        serial = "0123456789abcdef"
        create = _FakeWin32Function(wintypes.HANDLE(high_handle))
        flush = _FakeWin32Function(1)
        close = _FakeWin32Function(1)
        last_error = _FakeWin32Function(0)
        observed_identity_handles: list[int] = []

        def identity(handle: object) -> str:
            self.assertIsInstance(handle, wintypes.HANDLE)
            observed_identity_handles.append(int(handle.value))
            return f"windows:{serial}:" + "0" * 32

        with patch.object(
            ctypes.windll.kernel32, "CreateFileW", create
        ), patch.object(
            ctypes.windll.kernel32, "FlushFileBuffers", flush
        ), patch.object(
            ctypes.windll.kernel32, "CloseHandle", close
        ), patch.object(
            ctypes.windll.kernel32, "GetLastError", last_error
        ), patch(
            "app.reset_database._windows_handle_identity",
            side_effect=identity,
        ):
            self.real_volume_flush(
                "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
                serial,
            )

        self.assertEqual(create.argtypes, [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ])
        self.assertIs(create.restype, wintypes.HANDLE)
        self.assertEqual(flush.argtypes, [wintypes.HANDLE])
        self.assertIs(flush.restype, wintypes.BOOL)
        self.assertEqual(close.argtypes, [wintypes.HANDLE])
        self.assertIs(close.restype, wintypes.BOOL)
        self.assertEqual(last_error.argtypes, [])
        self.assertIs(last_error.restype, wintypes.DWORD)
        self.assertEqual(observed_identity_handles, [high_handle])
        self.assertEqual([int(call[0].value) for call in flush.calls], [high_handle])
        self.assertEqual([int(call[0].value) for call in close.calls], [high_handle])
        self.assertEqual(last_error.calls, [])

    def test_volume_create_invalid_and_null_handles_fail_closed(self) -> None:
        invalid = ctypes.c_void_p(-1).value
        for label, returned in (
            ("invalid", wintypes.HANDLE(invalid)),
            ("null", wintypes.HANDLE()),
        ):
            with self.subTest(label=label):
                create = _FakeWin32Function(returned)
                flush = _FakeWin32Function(1)
                close = _FakeWin32Function(1)
                last_error = _FakeWin32Function(5)
                with patch.object(
                    ctypes.windll.kernel32, "CreateFileW", create
                ), patch.object(
                    ctypes.windll.kernel32, "FlushFileBuffers", flush
                ), patch.object(
                    ctypes.windll.kernel32, "CloseHandle", close
                ), patch.object(
                    ctypes.windll.kernel32, "GetLastError", last_error
                ), patch(
                    "app.reset_database._windows_handle_identity"
                ) as identity:
                    with self.assertRaises(DatabaseResetError) as failed:
                        self.real_volume_flush(
                            "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
                            "0123456789abcdef",
                        )
                self.assertEqual(
                    failed.exception.code, "reset_durability_unsupported"
                )
                identity.assert_not_called()
                self.assertEqual(flush.calls, [])
                self.assertEqual(close.calls, [])
                self.assertEqual(len(last_error.calls), 1)

    def test_volume_flush_and_close_failures_map_to_sanitized_error(self) -> None:
        high_handle = 0x1234567887654321
        serial = "0123456789abcdef"
        for label, flush_result, close_result in (
            ("flush", 0, 1),
            ("close", 1, 0),
        ):
            with self.subTest(label=label):
                create = _FakeWin32Function(wintypes.HANDLE(high_handle))
                flush = _FakeWin32Function(flush_result)
                close = _FakeWin32Function(close_result)
                last_error = _FakeWin32Function(5)
                with patch.object(
                    ctypes.windll.kernel32, "CreateFileW", create
                ), patch.object(
                    ctypes.windll.kernel32, "FlushFileBuffers", flush
                ), patch.object(
                    ctypes.windll.kernel32, "CloseHandle", close
                ), patch.object(
                    ctypes.windll.kernel32, "GetLastError", last_error
                ), patch(
                    "app.reset_database._windows_handle_identity",
                    return_value=f"windows:{serial}:" + "0" * 32,
                ):
                    with self.assertRaises(DatabaseResetError) as failed:
                        self.real_volume_flush(
                            "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
                            serial,
                        )
                self.assertEqual(
                    failed.exception.code, "reset_durability_unsupported"
                )
                self.assertEqual(
                    [int(call[0].value) for call in flush.calls], [high_handle]
                )
                self.assertEqual(
                    [int(call[0].value) for call in close.calls], [high_handle]
                )
                self.assertEqual(
                    len(last_error.calls), 1
                )

    def test_preliminary_volume_failure_has_zero_sqlite_or_artifacts(self) -> None:
        database_before = self.database.read_bytes()
        controls_before = reset_v2._enumerate_reserved(self.root / "data")
        self.assertFalse((self.root / "backups").exists())
        unsupported = DatabaseResetError(
            "reset_durability_unsupported", "synthetic volume failure"
        )
        with patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=self.real_probe_durability,
        ), patch(
            "app.reset_protocol_v2._open_and_flush_volume",
            side_effect=unsupported,
        ) as volume_flush, patch(
            "app.reset_protocol_v2.sqlite3.connect",
            side_effect=AssertionError("SQLite must remain unopened"),
        ) as protocol_sqlite, patch(
            "app.reset_database.sqlite3.connect",
            side_effect=AssertionError("SQLite must remain unopened"),
        ) as legacy_sqlite:
            with self.assertRaises(DatabaseResetError) as failed:
                plan_database_reset("data/helios.db", repository_root=self.root)
        self.assertEqual(failed.exception.code, "reset_durability_unsupported")
        volume_flush.assert_called_once()
        protocol_sqlite.assert_not_called()
        legacy_sqlite.assert_not_called()
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual(
            reset_v2._enumerate_reserved(self.root / "data"), controls_before
        )
        self.assertFalse((self.root / "backups").exists())

    def test_preliminary_directory_flush_does_not_open_volume(self) -> None:
        (self.root / "backups").mkdir()
        with patch(
            "app.reset_protocol_v2._open_and_flush_volume",
            side_effect=AssertionError("directory mode must not open volume"),
        ) as volume_flush:
            durability = self.real_probe_durability(self.root)
        self.assertEqual(durability["mode"], "directory_flush")
        self.assertIsNone(durability["fallback_reason"])
        self.assertTrue(all(
            item["flush_succeeded"] is True
            for item in durability["directory_probes"]
        ))
        volume_flush.assert_not_called()

    def test_legacy_state_rejects_external_native_manifest_before_open(self) -> None:
        token = "d" * 64
        backup = "backups/helios-pre-room-shared-reset-20260814T120000003Z.db"
        audit = "backups/helios-pre-room-shared-reset-20260814T120000003Z.audit.json"
        legacy_path = self.make_legacy_ready_journal(token, backup, audit)
        with patch(
            "app.reset_protocol_v2._read_reviewed_plan",
            side_effect=AssertionError("legacy state must not open a native manifest"),
        ) as manifest_open:
            with self.assertRaises(DatabaseResetError) as invalid:
                recover_database_reset(
                    "data/helios.db", expected_plan_token=token,
                    action="restore-source", confirm_reset_recovery=True,
                    reviewed_plan_manifest="C:\\synthetic\\reviewed.json",
                    repository_root=self.root,
                )
        self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
        manifest_open.assert_not_called()
        self.assertTrue(legacy_path.is_file())

    def test_stale_or_unconfirmed_execution_fails_before_mutation(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        before = self.database.read_bytes()
        with self.assertRaises(DatabaseResetError) as missing:
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=False, repository_root=self.root,
            )
        self.assertEqual(missing.exception.code, "reset_confirmation_required")
        self.assertEqual(self.database.read_bytes(), before)
        with self.assertRaises(DatabaseResetError) as stale:
            execute_database_reset(
                "data/helios.db", expected_plan_token="f" * 64,
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
            )
        self.assertEqual(stale.exception.code, "reset_plan_stale")
        self.assertEqual(self.database.read_bytes(), before)

    def test_post_generation_durability_failure_retains_explicit_recovery_interlock(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        calls = 0

        def fail_generation_flush(_authority: object, _parent: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise DatabaseResetError("reset_failed", "synthetic durable boundary")

        with patch(
            "app.reset_protocol_v2._flush_namespace",
            side_effect=fail_generation_flush,
        ):
            with self.assertRaises(DatabaseResetError) as failed:
                execute_database_reset(
                    "data/helios.db", expected_plan_token=plan["plan_token"],
                    expected_backup_path=plan["backup_path"],
                    expected_audit_path=plan["audit_path"],
                    confirm_destroy_canonical_history=True,
                    repository_root=self.root,
                )
        self.assertEqual(failed.exception.code, "reset_failed")
        controls = [
            path for path in (self.root / "data").iterdir()
            if path.name.startswith(".helios-room-reset-state.")
        ]
        self.assertEqual(len(controls), 1)
        self.assertTrue(reset_v2.GENERATION_RE.fullmatch(controls[0].name))
        with self.assertRaises(ResetRecoveryRequiredError):
            connect_database(self.database)

    def test_post_generation_non_durability_failure_invokes_safe_source_restore(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with patch(
            "app.reset_protocol_v2.validate_v14_foundation",
            side_effect=RuntimeError("synthetic validation failure"),
        ):
            with self.assertRaises(DatabaseResetError) as failed:
                execute_database_reset(
                    "data/helios.db", expected_plan_token=plan["plan_token"],
                    expected_backup_path=plan["backup_path"],
                    expected_audit_path=plan["audit_path"],
                    confirm_destroy_canonical_history=True,
                    repository_root=self.root,
                )
        self.assertEqual(failed.exception.code, "reset_failed")
        self.assertFalse(any(
            path.name.startswith(".helios-room-reset-state.")
            for path in (self.root / "data").iterdir()
        ))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_label FROM schema_migrations "
                    "ORDER BY migration_no DESC LIMIT 1"
                ).fetchone()[0],
                "1.2",
            )
        finally:
            connection.close()

    def test_recovery_interlock_precedes_every_ordinary_sqlite_open(self) -> None:
        state = self.root / "data" / ".helios-room-reset-state.json"
        state.write_text("synthetic control state\n", encoding="utf-8")
        with patch(
            "app.database.sqlite3.connect",
            side_effect=AssertionError("recovery interlock must precede SQLite"),
        ) as sqlite_open:
            with self.assertRaises(ResetRecoveryRequiredError):
                connect_database(self.database)
            with self.assertRaises(DatabaseInitializationError) as initialized:
                initialize_database(self.database)
            with self.assertRaises(DatabaseMigrationError) as migrated:
                migrate_database(self.database)
            with self.assertRaises(DatabasePreflightError) as preflighted:
                preflight_database(self.database)
            with self.assertRaises(DatabaseResetError) as planned:
                plan_database_reset("data/helios.db", repository_root=self.root)
        self.assertEqual(sqlite_open.call_count, 0)
        for error in (
            initialized.exception,
            migrated.exception,
            preflighted.exception,
            planned.exception,
        ):
            self.assertEqual(error.code, "database_reset_recovery_required")

    def test_authorized_fixture_execution_retains_backup_audit_and_fresh_v14(self) -> None:
        before_status = self.status()
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        report = execute_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            expected_backup_path=plan["backup_path"],
            expected_audit_path=plan["audit_path"],
            confirm_destroy_canonical_history=True, repository_root=self.root,
        )
        self.assertEqual(report["status"], "reset")
        self.assertEqual(report["old_schema_label"], "1.2")
        self.assertEqual(report["schema_label"], "1.4")
        self.assertTrue((self.root / report["backup_path"]).is_file())
        audit = json.loads((self.root / report["audit_path"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["outcome"], "reset")
        self.assertNotIn("message_text", json.dumps(audit))
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            validate_v14_foundation(connection)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 0)
        finally:
            connection.close()
        self.assertEqual(self.status(), before_status)
        self.assertFalse((self.root / "data" / ".helios-room-reset-state.json").exists())

    def test_crash_journal_blocks_startup_and_restore_source_is_repeatable(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (_ for _ in ()).throw(KeyboardInterrupt())
                if stage == "original_quarantined" else None,
            )
        generations = [
            path for path in (self.root / "data").iterdir()
            if reset_v2.GENERATION_RE.fullmatch(path.name)
        ]
        self.assertGreaterEqual(len(generations), 1)
        parsed = sorted(
            (reset_v2._read_generation(path, partial=False) for path in generations),
            key=lambda item: item.sequence,
        )
        self.assertEqual(
            [item.sequence for item in parsed], list(range(1, len(parsed) + 1))
        )
        self.assertIsNone(parsed[0].envelope["previous_generation_sha256"])
        for index, generation in enumerate(parsed):
            self.assertEqual(set(generation.envelope), {
                "converter_implementation_commit", "generation_sequence",
                "generation_version", "journal", "legacy_recovery_evidence",
                "legacy_source", "origin", "previous_generation_sha256",
                "reviewed_plan_manifest",
            })
            self.assertEqual(generation.envelope["origin"], "native_v2")
            self.assertEqual(
                generation.envelope["reviewed_plan_manifest"],
                plan["reviewed_plan_manifest"],
            )
            if index:
                self.assertEqual(
                    generation.envelope["previous_generation_sha256"],
                    parsed[index - 1].content_hash,
                )
        stale_partial = Path(f"{parsed[0].path}.partial")
        stale_partial.write_bytes(parsed[0].raw)
        self.assertFalse((self.root / "data" / ".helios-room-reset-state.json").exists())
        with self.assertRaises(ResetRecoveryRequiredError):
            connect_database(self.database)
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(stale_partial.exists())
        self.assertFalse(any(
            path.name.startswith(".helios-room-reset-state.")
            for path in (self.root / "data").iterdir()
        ))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute(
                "SELECT schema_label FROM schema_migrations ORDER BY migration_no DESC LIMIT 1"
            ).fetchone()[0], "1.2")
        finally:
            connection.close()

    def test_incomplete_first_generation_requires_reviewed_plan_and_aborts_safely(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "ready_to_quarantine:generation_partial_write" else None
                ),
            )
        partials = [
            path for path in (self.root / "data").iterdir()
            if path.name.endswith(".json.partial")
        ]
        self.assertEqual(len(partials), 1)
        with self.assertRaises(ResetRecoveryRequiredError):
            connect_database(self.database)
        with self.assertRaises(DatabaseResetError) as missing:
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
            )
        self.assertEqual(missing.exception.code, "reset_recovery_invalid")
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).parents[1]
        ) as evidence_directory:
            reviewed = Path(evidence_directory) / "reviewed-plan.json"
            reviewed.write_bytes(reset_v2._canonical_bytes(plan))
            original_open = reset_v2._open_reviewed_plan_handle
            reviewed_opens = 0

            def count_reviewed_open(path: Path) -> int:
                nonlocal reviewed_opens
                if Path(path) == reviewed:
                    reviewed_opens += 1
                return original_open(path)

            with patch(
                "app.reset_protocol_v2._open_reviewed_plan_handle",
                side_effect=count_reviewed_open,
            ):
                report = recover_database_reset(
                    "data/helios.db", expected_plan_token=plan["plan_token"],
                    action="restore-source", confirm_reset_recovery=True,
                    reviewed_plan_manifest=str(reviewed), repository_root=self.root,
                )
            self.assertEqual(reviewed_opens, 1)
        self.assertEqual(report["status"], "prejournal_aborted")
        self.assertFalse(partials[0].exists())

    def test_invalid_completed_generation_fails_closed_before_recovery_mutation(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "fresh_installed" else None
                ),
            )
        generations = sorted(
            (path for path in (self.root / "data").iterdir()
             if reset_v2.GENERATION_RE.fullmatch(path.name)),
            key=lambda path: path.name,
        )
        self.assertGreater(len(generations), 1)
        target = generations[-1]
        original = target.read_bytes()
        target.write_bytes(original[:-2] + b"x\n")
        database_before = self.database.read_bytes()
        with self.assertRaises(DatabaseResetError) as invalid:
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
            )
        self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
        self.assertEqual(self.database.read_bytes(), database_before)
        target.write_bytes(original)
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(any((self.root / "data").glob("*.restoring")))
        self.assertFalse(any((self.root / "data").glob("*.failed-new")))

    def test_incomplete_backup_copy_is_removed_and_regenerated(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "fresh_installed" else None
                ),
            )
        original = self.root / plan["reviewed_plan_manifest"]["artifact_paths"]["original_quarantine_database_path"]
        original.unlink()
        with self.assertRaises(KeyboardInterrupt):
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "recovery:backup_copy_partial" else None
                ),
            )
        copy_partial = self.root / plan["reviewed_plan_manifest"]["artifact_paths"]["backup_copy_partial_path"]
        restoring = self.root / plan["reviewed_plan_manifest"]["artifact_paths"]["restoring_database_path"]
        backup = self.root / plan["backup_path"]
        self.assertGreater(copy_partial.stat().st_size, 0)
        self.assertLess(copy_partial.stat().st_size, backup.stat().st_size)
        self.assertFalse(restoring.exists())
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(copy_partial.exists())
        self.assertFalse(restoring.exists())

    def test_recovery_rejects_byte_identical_replacement_backup_identity(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True,
                repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "fresh_installed" else None
                ),
            )
        backup = self.root / plan["backup_path"]
        original_bytes = backup.read_bytes()
        recorded_identity = reset_module._path_identity(backup)
        replacement = backup.with_name(backup.name + ".replacement")
        replacement.write_bytes(original_bytes)
        backup.unlink()
        replacement.rename(backup)
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.assertNotEqual(reset_module._path_identity(backup), recorded_identity)
        database_before = self.database.read_bytes()
        controls_before = {
            path.name: path.read_bytes()
            for path in (self.root / "data").iterdir()
            if path.name.startswith(".helios-room-reset-state.")
        }
        with self.assertRaises(DatabaseResetError) as invalid:
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="restore-source", confirm_reset_recovery=True,
                repository_root=self.root,
            )
        self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
        self.assertEqual(self.database.read_bytes(), database_before)
        self.assertEqual({
            path.name: path.read_bytes()
            for path in (self.root / "data").iterdir()
            if path.name.startswith(".helios-room-reset-state.")
        }, controls_before)

    def test_destructive_helpers_reject_links_and_parent_substitution(self) -> None:
        hard_link = self.root / "data" / (".helios.db.reset-" + "0" * 64 + ".failed-new")
        os.link(self.database, hard_link)
        try:
            with self.assertRaises(DatabaseResetError) as hard_linked:
                plan_database_reset("data/helios.db", repository_root=self.root)
            self.assertEqual(hard_linked.exception.code, "reset_path_unsafe")
        finally:
            hard_link.unlink()

        staging = self.root / "data" / ".race-source"
        target = self.root / "data" / ".race-target"
        staging.write_bytes(b"synthetic")
        identity = reset_module._path_identity(staging)
        original = reset_module._revalidate_parent
        calls = 0

        def substitute(path: Path, expected: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DatabaseResetError(
                    "reset_path_unsafe", "synthetic parent substitution"
                )
            original(path, expected)

        with patch("app.reset_database._revalidate_parent", side_effect=substitute):
            with self.assertRaises(DatabaseResetError) as raced:
                reset_module._move_verified(staging, target, identity)
        self.assertEqual(raced.exception.code, "reset_path_unsafe")
        self.assertTrue(staging.is_file())
        self.assertFalse(target.exists())
        staging.unlink()

        symlink = self.root / "data" / ".unsafe-link"
        try:
            symlink.symlink_to(self.database)
        except OSError:
            pass
        else:
            with self.assertRaises(DatabaseResetError):
                reset_module._install_no_overwrite(symlink, target)
            symlink.unlink()

    def test_source_entry_substitution_cannot_rename_or_delete_attacker_file(self) -> None:
        for operation in ("rename", "unlink"):
            with self.subTest(operation=operation):
                source = self.root / "data" / f".source-{operation}"
                displaced = self.root / "data" / f".displaced-{operation}"
                target = self.root / "data" / f".target-{operation}"
                source.write_bytes(b"expected-source")
                identity = reset_module._path_identity(source)
                original_revalidation = reset_module._revalidate_source_entry
                swapped = False

                def substitute(path: Path, parent_handle: int, expected: str) -> None:
                    nonlocal swapped
                    if not swapped:
                        swapped = True
                        os.replace(path, displaced)
                        path.write_bytes(b"attacker-file")
                    original_revalidation(path, parent_handle, expected)

                with patch(
                    "app.reset_database._revalidate_source_entry",
                    side_effect=substitute,
                ):
                    with self.assertRaises(DatabaseResetError) as rejected:
                        if operation == "rename":
                            reset_module._move_verified(source, target, identity)
                        else:
                            reset_module._unlink_verified(source, identity)
                self.assertEqual(rejected.exception.code, "reset_path_unsafe")
                self.assertEqual(source.read_bytes(), b"attacker-file")
                self.assertEqual(displaced.read_bytes(), b"expected-source")
                self.assertFalse(target.exists())
                source.unlink()
                displaced.unlink()

    @unittest.skipUnless(os.name == "nt", "handle-bound mutation is Windows-specific")
    def test_windows_mutation_remains_bound_after_source_or_target_substitution(self) -> None:
        source = self.root / "data" / ".source-after-validation"
        displaced = self.root / "data" / ".displaced-after-validation"
        target = self.root / "data" / ".target-after-validation"
        source.write_bytes(b"expected-source")
        source_identity = reset_module._path_identity(source)
        original_rename = reset_module._windows_rename

        def swap_source_then_rename(
            handle: int, destination: Path, parent: int, *, replace: bool
        ) -> None:
            os.replace(source, displaced)
            source.write_bytes(b"attacker-source")
            original_rename(handle, destination, parent, replace=replace)

        with patch(
            "app.reset_database._windows_rename",
            side_effect=swap_source_then_rename,
        ):
            reset_module._move_verified(source, target, source_identity)
        self.assertEqual(source.read_bytes(), b"attacker-source")
        self.assertEqual(target.read_bytes(), b"expected-source")
        self.assertFalse(displaced.exists())
        source.unlink()
        target.unlink()

        source.write_bytes(b"expected-source")
        source_identity = reset_module._path_identity(source)

        def occupy_target_then_rename(
            handle: int, destination: Path, parent: int, *, replace: bool
        ) -> None:
            self.assertFalse(replace)
            target.write_bytes(b"attacker-target")
            original_rename(handle, destination, parent, replace=replace)

        with patch(
            "app.reset_database._windows_rename",
            side_effect=occupy_target_then_rename,
        ):
            with self.assertRaises(DatabaseResetError) as collision:
                reset_module._move_verified(source, target, source_identity)
        self.assertEqual(collision.exception.code, "reset_failed")
        self.assertEqual(source.read_bytes(), b"expected-source")
        self.assertEqual(target.read_bytes(), b"attacker-target")
        source.unlink()
        target.unlink()

        source.write_bytes(b"expected-source")
        source_identity = reset_module._path_identity(source)
        original_unlink = reset_module._windows_unlink_handle

        def swap_source_then_unlink(handle: int) -> None:
            os.replace(source, displaced)
            source.write_bytes(b"attacker-source")
            original_unlink(handle)

        with patch(
            "app.reset_database._windows_unlink_handle",
            side_effect=swap_source_then_unlink,
        ):
            reset_module._unlink_verified(source, source_identity)
        self.assertEqual(source.read_bytes(), b"attacker-source")
        self.assertFalse(displaced.exists())
        source.unlink()

    @unittest.skipUnless(os.name == "nt", "junction semantics are Windows-specific")
    def test_reset_rejects_junction_database_parent(self) -> None:
        data = self.root / "data"
        (self.root / "backups").mkdir()
        real_data = self.root / "backups" / "junction-data"
        os.replace(data, real_data)
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(data), str(real_data)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if created.returncode != 0:
            os.replace(real_data, data)
            self.skipTest("junction creation is unavailable")
        try:
            with self.assertRaises(DatabaseResetError) as unsafe:
                plan_database_reset("data/helios.db", repository_root=self.root)
            self.assertEqual(unsafe.exception.code, "reset_path_unsafe")
        finally:
            os.rmdir(data)
            os.replace(real_data, data)

    def test_crash_after_fresh_validation_can_only_complete_fresh(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (_ for _ in ()).throw(KeyboardInterrupt())
                if stage == "fresh_validated" else None,
            )
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="complete-fresh", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "reset")
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            validate_v14_foundation(connection)
        finally:
            connection.close()

    def test_complete_fresh_recovers_from_interrupted_audit_write(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "fresh_validated" else None
                ),
            )
        with self.assertRaises(KeyboardInterrupt):
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="complete-fresh", confirm_reset_recovery=True,
                repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "recovery:audit_write_partial" else None
                ),
            )
        partial = Path(f"{self.root / plan['audit_path']}.partial")
        with self.assertRaises((json.JSONDecodeError, UnicodeDecodeError)):
            json.loads(partial.read_text(encoding="utf-8"))
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="complete-fresh", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "reset")
        self.assertFalse(partial.exists())

    def test_recovery_schema_failure_is_sanitized_not_name_error(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "fresh_validated" else None
                ),
            )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP VIEW current_room_participants")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(DatabaseResetError) as invalid:
            recover_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                action="complete-fresh", confirm_reset_recovery=True,
                repository_root=self.root,
            )
        self.assertEqual(invalid.exception.code, "reset_recovery_invalid")

    def test_every_durable_stage_remains_ignored_and_explicitly_recoverable(self) -> None:
        stages = (
            "ready_to_quarantine", "quarantining", "original_quarantined",
            "installing_fresh", "fresh_installed", "validating_fresh",
            "fresh_validated", "cleaning_quarantine", "finalizing",
        )
        for sequence, stage in enumerate(stages, start=1):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                dir=Path(__file__).parents[1]
            ) as directory:
                root = Path(directory)
                (root / "data").mkdir()
                (root / ".gitignore").write_text(IGNORE_BLOCK, encoding="utf-8")
                (root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
                for arguments in (
                    ("init",),
                    ("config", "user.email", "reset@example.invalid"),
                    ("config", "user.name", "Reset Test"),
                    ("add", ".gitignore", "tracked.txt"),
                    ("commit", "-m", "fixture"),
                ):
                    subprocess.run(
                        ["git", "-C", str(root), *arguments],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                    )
                database = root / "data" / "helios.db"
                database.write_bytes(self.database.read_bytes())
                status_before = subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain=v1",
                     "--untracked-files=all", "--ignore-submodules=none"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                ).stdout
                plan = plan_database_reset("data/helios.db", repository_root=root)
                with self.assertRaises(KeyboardInterrupt):
                    execute_database_reset(
                        "data/helios.db",
                        expected_plan_token=plan["plan_token"],
                        expected_backup_path=plan["backup_path"],
                        expected_audit_path=plan["audit_path"],
                        confirm_destroy_canonical_history=True,
                        repository_root=root,
                        crash_checkpoint=lambda reached, target=stage: (
                            (_ for _ in ()).throw(KeyboardInterrupt())
                            if reached == target else None
                        ),
                    )
                generations = [
                    reset_v2._read_generation(path, partial=False)
                    for path in (root / "data").iterdir()
                    if reset_v2.GENERATION_RE.fullmatch(path.name)
                ]
                latest = max(generations, key=lambda item: item.sequence)
                journal = latest.envelope["journal"]
                self.assertEqual(journal["stage"], stage)
                self.assertEqual(journal["journal_sequence"], latest.sequence)
                action = (
                    "complete-fresh"
                    if stage in {"fresh_validated", "cleaning_quarantine", "finalizing"}
                    else "restore-source"
                )
                report = recover_database_reset(
                    "data/helios.db", expected_plan_token=plan["plan_token"],
                    action=action, confirm_reset_recovery=True,
                    repository_root=root,
                )
                self.assertEqual(
                    report["status"], "reset" if action == "complete-fresh" else "restored_source"
                )
                status_after = subprocess.run(
                    ["git", "-C", str(root), "status", "--porcelain=v1",
                     "--untracked-files=all", "--ignore-submodules=none"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                ).stdout
                self.assertEqual(status_after, status_before)

    def test_external_manifest_uses_exact_root_parent_and_leaf_access(self) -> None:
        snapshots = {
            handle: {
                "delete_pending": False,
                "directory": handle != 12,
                "file_attributes": 0x10 if handle != 12 else 0x80,
                "file_id": f"windows:{1:016x}:{handle:032x}",
                "link_count": 1,
                "reparse_tag": 0,
            }
            for handle in (10, 11, 12)
        }
        with patch.object(
            reset_module, "_windows_open_handle", return_value=10
        ) as root_open, patch.object(
            reset_module, "_windows_create_relative", side_effect=(11, 12)
        ) as relative_open, patch.object(
            reset_module, "_windows_validate_handle"
        ), patch.object(
            reset_module, "_windows_handle_snapshot",
            side_effect=lambda handle: snapshots[handle],
        ), patch.object(reset_module, "_windows_close_handle") as close:
            handle = reset_v2._open_reviewed_plan_handle(
                Path("C:\\parent\\reviewed.json")
            )
            self.assertEqual(handle, 12)
            root_open.assert_called_once_with(
                Path("C:\\"), 0x80000000, 0x02000000 | 0x00200000,
                share_access=0x1 | 0x2 | 0x4,
            )
            self.assertEqual(relative_open.call_count, 2)
            parent_call, leaf_call = relative_open.call_args_list
            self.assertEqual(parent_call.args, (10, "parent"))
            self.assertEqual(
                parent_call.kwargs,
                {
                    "directory": True,
                    "create_new": False,
                    "desired_access": 0x00100000 | 0x00000001 | 0x00000080,
                    "share_access": 0x1 | 0x2 | 0x4,
                },
            )
            self.assertEqual(leaf_call.args, (11, "reviewed.json"))
            self.assertEqual(
                leaf_call.kwargs,
                {
                    "directory": False,
                    "create_new": False,
                    "desired_access": 0x00100000 | 0x00000001 | 0x00000080,
                    "share_access": 0x1 | 0x4,
                },
            )
            self.assertEqual(close.call_count, 2)

    def test_generation_grammar_envelope_and_chain_are_closed(self) -> None:
        token = "a" * 64
        digest = "b" * 64
        valid_names = (
            reset_v2._generation_name(token, 1, digest),
            reset_v2._generation_name(token, 4096, digest),
        )
        self.assertTrue(all(reset_v2.GENERATION_RE.fullmatch(name) for name in valid_names))
        invalid_names = (
            f".helios-room-reset-state.{token}.g0000000000000000000.{digest}.json",
            f".helios-room-reset-state.{token.upper()}.g00000000000000000001.{digest}.json",
            f".helios-room-reset-state.{token}.g00000000000000000001.{digest.upper()}.json",
            f".helios-room-reset-state.{token}.g00000000000000000001.{digest}.JSON",
            f".helios-room-reset-state.{token}.g00000000000000000001.{digest}.json.extra",
            f".helios-room-reset-state.{token}．g00000000000000000001.{digest}.json",
        )
        self.assertTrue(all(reset_v2.GENERATION_RE.fullmatch(name) is None for name in invalid_names))

        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db",
                expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True,
                repository_root=self.root,
                crash_checkpoint=lambda reached: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if reached == "ready_to_quarantine" else None
                ),
            )
        generation_one = next(
            reset_v2._read_generation(path, partial=False)
            for path in (self.root / "data").iterdir()
            if reset_v2.GENERATION_RE.fullmatch(path.name)
        )
        store = reset_v2._store_from_chain(self.root, [generation_one])
        journal = store.journal
        store.append(journal, journal["stage"])
        generation_two = store.generations[-1]
        commit = plan["implementation_commit"]
        self.assertEqual(
            reset_v2._validate_chain(store.generations, plan["plan_token"], commit),
            store.generations,
        )

        def changed_generation(**changes: object) -> reset_v2.Generation:
            values = {
                "path": generation_two.path,
                "partial": False,
                "token": generation_two.token,
                "sequence": generation_two.sequence,
                "content_hash": generation_two.content_hash,
                "envelope": generation_two.envelope,
                "raw": generation_two.raw,
                "identity": generation_two.identity,
            }
            values.update(changes)
            return reset_v2.Generation(**values)

        invalid_chains = (
            [generation_one, changed_generation(sequence=1)],
            [generation_one, changed_generation(sequence=3)],
            [generation_one, changed_generation(token="f" * 64)],
            [
                generation_one,
                changed_generation(
                    envelope={
                        **generation_two.envelope,
                        "previous_generation_sha256": "f" * 64,
                    }
                ),
            ],
            [
                generation_one,
                changed_generation(
                    envelope={**generation_two.envelope, "origin": "legacy_v1_conversion"}
                ),
            ],
        )
        for chain in invalid_chains:
            with self.subTest(chain=chain[1].sequence), self.assertRaises(
                DatabaseResetError
            ) as invalid:
                reset_v2._validate_chain(chain, plan["plan_token"], commit)
            self.assertEqual(invalid.exception.code, "reset_recovery_invalid")

        envelope = json.loads(json.dumps(generation_one.envelope))
        envelope["generation_sequence"] = 4097
        envelope["journal"]["journal_sequence"] = 4097
        envelope["previous_generation_sha256"] = "c" * 64
        with self.assertRaises(DatabaseResetError) as overflow:
            reset_v2._validate_envelope(
                envelope,
                token=plan["plan_token"],
                content_hash=reset_v2._sha256(reset_v2._canonical_bytes(envelope)),
                sequence=4097,
            )
        self.assertEqual(overflow.exception.code, "reset_recovery_invalid")

    def test_persisted_durability_distinguishes_mismatch_from_operation_failure(self) -> None:
        persisted = self.synthetic_durability(self.root)
        stable = self.synthetic_stable_authority(self.root)
        mismatched = dict(stable)
        mismatched["volume_guid"] = (
            "\\\\?\\Volume{00000000-0000-0000-0000-000000000002}\\"
        )
        with patch(
            "app.reset_protocol_v2._inspect_stable_durability_authority",
            return_value=mismatched,
        ), patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=AssertionError("mode probe must follow stable validation"),
        ) as probe:
            with self.assertRaises(DatabaseResetError) as invalid:
                reset_v2._revalidate_persisted_authority(self.root, persisted)
        self.assertEqual(invalid.exception.code, "reset_recovery_invalid")
        probe.assert_not_called()

        with patch(
            "app.reset_protocol_v2._inspect_stable_durability_authority",
            return_value=stable,
        ), patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=AssertionError("persisted mode must not be reselected"),
        ) as probe, patch(
            "app.reset_protocol_v2._open_and_flush_volume",
            side_effect=DatabaseResetError(
                "reset_durability_unsupported", "synthetic operation failure"
            ),
        ) as volume_flush:
            with self.assertRaises(DatabaseResetError) as failed:
                reset_v2._revalidate_persisted_authority(self.root, persisted)
        self.assertEqual(failed.exception.code, "reset_failed")
        probe.assert_not_called()
        volume_flush.assert_called_once_with(
            persisted["volume_guid"], persisted["handle_volume_serial_hex"]
        )

        (self.root / "backups").mkdir()
        directory_persisted = self.synthetic_durability(self.root)
        current_stable = self.synthetic_stable_authority(self.root)
        directory_calls: list[tuple[str, Path, str]] = []

        def directory_flush(
            name: str, path: Path, identity: str
        ) -> dict[str, object]:
            directory_calls.append((name, path, identity))
            return {
                "error_code": None,
                "flush_succeeded": True,
                "identity": identity,
                "name": name,
                "state": "present",
            }

        with patch(
            "app.reset_protocol_v2._inspect_stable_durability_authority",
            return_value=current_stable,
        ), patch(
            "app.reset_protocol_v2._probe_durability",
            side_effect=AssertionError("persisted mode must not be reselected"),
        ) as probe, patch(
            "app.reset_protocol_v2._directory_probe",
            side_effect=directory_flush,
        ), patch(
            "app.reset_protocol_v2._open_and_flush_volume",
            side_effect=AssertionError("directory mode must not flush volume"),
        ) as volume_flush:
            self.assertIs(
                reset_v2._revalidate_persisted_authority(
                    self.root, directory_persisted
                ),
                directory_persisted,
            )
        probe.assert_not_called()
        volume_flush.assert_not_called()
        self.assertEqual(
            [item[0] for item in directory_calls],
            ["repository", "data", "backups"],
        )

    def test_real_subprocess_generation_interruption_matrix(self) -> None:
        checkpoints = (
            "ready_to_quarantine:generation_partial_write",
            "ready_to_quarantine:generation_file_durable",
            "ready_to_quarantine:generation_before_promotion",
            "ready_to_quarantine:generation_promoted",
            "ready_to_quarantine",
            "quarantining:generation_partial_write",
        )
        child = r'''
import os
import sys
from pathlib import Path
from unittest.mock import patch
import app.reset_database as legacy
import app.reset_protocol_v2 as protocol

root = Path(sys.argv[1])
token, backup, audit, checkpoint = sys.argv[2:6]

def durability(value):
    repository = legacy._path_identity(value)
    data = legacy._path_identity(value / "data")
    backups = value / "backups"
    backup_identity = legacy._path_identity(backups) if backups.exists() else None
    serial = repository.split(":")[1]
    return {
        "api_contract": "win32_flush_v1",
        "directory_probes": [
            {"error_code": None, "flush_succeeded": True, "identity": repository, "name": "repository", "state": "present"},
            {"error_code": None, "flush_succeeded": True, "identity": data, "name": "data", "state": "present"},
            ({"error_code": None, "flush_succeeded": True, "identity": backup_identity, "name": "backups", "state": "present"}
             if backup_identity is not None else
             {"error_code": None, "flush_succeeded": None, "identity": None, "name": "backups", "state": "absent"}),
        ],
        "drive_type": 3,
        "fallback_reason": None if backup_identity is not None else "backups_absent",
        "filesystem_name": "NTFS",
        "handle_volume_serial_hex": serial,
        "mode": "directory_flush" if backup_identity is not None else "volume_flush",
        "volume_flush_succeeded": None if backup_identity is not None else True,
        "volume_guid": "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\",
        "volume_information_serial_hex": "00000001",
        "volume_opened": backup_identity is None,
        "volume_root_resolved": True,
    }

def terminate(reached):
    if reached == checkpoint:
        os._exit(91)

with patch("app.reset_protocol_v2._probe_durability", side_effect=lambda value: durability(Path(value))), \
     patch("app.reset_protocol_v2._flush_namespace"), \
     patch("app.reset_protocol_v2._open_and_flush_volume"):
    protocol.execute_database_reset(
        "data/helios.db",
        expected_plan_token=token,
        expected_backup_path=backup,
        expected_audit_path=audit,
        confirm_destroy_canonical_history=True,
        repository_root=root,
        crash_checkpoint=terminate,
    )
raise SystemExit(92)
'''
        python = Path(sys.executable)
        for index, checkpoint in enumerate(checkpoints):
            with self.subTest(checkpoint=checkpoint):
                root, database = self.make_isolated_fixture(f"subprocess-{index}")
                plan = plan_database_reset(
                    "data/helios.db", repository_root=root
                )
                reviewed = self.root / f"reviewed-{index}.json"
                reviewed.write_bytes(reset_v2._canonical_bytes(plan))
                result = subprocess.run(
                    [
                        str(python), "-c", child, str(root),
                        plan["plan_token"], plan["backup_path"],
                        plan["audit_path"], checkpoint,
                    ],
                    cwd=Path(__file__).parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=90,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 91,
                    msg=result.stderr.decode("utf-8", errors="replace"),
                )
                with self.assertRaises(ResetRecoveryRequiredError):
                    connect_database(database)
                report = recover_database_reset(
                    "data/helios.db",
                    expected_plan_token=plan["plan_token"],
                    action="restore-source",
                    confirm_reset_recovery=True,
                    reviewed_plan_manifest=str(reviewed),
                    repository_root=root,
                )
                self.assertIn(
                    report["status"], {"prejournal_aborted", "restored_source"}
                )
                self.assertFalse(reset_v2._enumerate_reserved(root / "data"))
                source = reset_module._open_reset_source(database)
                try:
                    self.assertEqual(reset_module._validate_source(source)[0], "1.2")
                finally:
                    source.rollback()
                    source.close()
                self.assertEqual(
                    subprocess.run(
                        [
                            "git", "-C", str(root), "status", "--porcelain=v1",
                            "--untracked-files=all", "--ignore-submodules=none",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=True,
                    ).stdout,
                    b"",
                )


if __name__ == "__main__":
    unittest.main()
