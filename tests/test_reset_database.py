from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.reset_database as reset_module

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


class ResetProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return result.stdout

    def make_v12(self) -> None:
        schema = Path(__file__).parents[1] / "schema" / "helios_room_schema_v1_2.sql"
        connection = sqlite3.connect(self.database)
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

    def status(self) -> str:
        return self.git(
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        )

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
            "recovery_commit_by_schema", "reset_protocol_version", "status",
        })
        self.assertEqual(plan["database_path"], "data/helios.db")
        self.assertEqual(plan["status"], "planned")
        self.assertRegex(plan["plan_token"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.database.read_bytes(), before_bytes)
        self.assertEqual(self.status(), before_status)

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
        state = self.root / "data" / ".helios-room-reset-state.json"
        self.assertTrue(state.is_file())
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(state.exists())
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute(
                "SELECT schema_label FROM schema_migrations ORDER BY migration_no DESC LIMIT 1"
            ).fetchone()[0], "1.2")
        finally:
            connection.close()

    def test_first_journal_next_only_is_authorized_recovery_input(self) -> None:
        plan = plan_database_reset("data/helios.db", repository_root=self.root)
        with self.assertRaises(KeyboardInterrupt):
            execute_database_reset(
                "data/helios.db", expected_plan_token=plan["plan_token"],
                expected_backup_path=plan["backup_path"],
                expected_audit_path=plan["audit_path"],
                confirm_destroy_canonical_history=True, repository_root=self.root,
                crash_checkpoint=lambda stage: (
                    (_ for _ in ()).throw(KeyboardInterrupt())
                    if stage == "ready_to_quarantine:next_durable" else None
                ),
            )
        state = self.root / "data" / ".helios-room-reset-state.json"
        next_path = Path(f"{state}.next")
        self.assertFalse(state.exists())
        self.assertTrue(next_path.is_file())
        with self.assertRaises(ResetRecoveryRequiredError):
            connect_database(self.database)
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(state.exists())
        self.assertFalse(next_path.exists())

    def test_restore_recovery_survives_process_termination_at_every_mutation_group(self) -> None:
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
        script = (
            "import os,sys\n"
            "from app.reset_database import recover_database_reset\n"
            "target=sys.argv[1]\n"
            "def crash(stage):\n"
            "    if stage == target: os._exit(73)\n"
            "recover_database_reset('data/helios.db', expected_plan_token=sys.argv[3], "
            "action='restore-source', confirm_reset_recovery=True, "
            "repository_root=sys.argv[2], crash_checkpoint=crash)\n"
        )
        checkpoints = (
            "recovery:journal:restore_quarantine:next_durable",
            "recovery:before_restore_original:database",
            "recovery:after_restore_original:database",
            "recovery:journal:restore_validate:next_durable",
            "recovery:journal:restore_audit:next_durable",
            "recovery:before_audit", "recovery:audit_write_partial",
            "recovery:audit_partial_durable",
            "recovery:after_audit",
            "recovery:journal:restore_cleanup:next_durable",
            "recovery:before_cleanup_failed:database",
            "recovery:after_cleanup_failed:database",
        )
        for checkpoint in checkpoints:
            result = subprocess.run(
                [sys.executable, "-c", script, checkpoint, str(self.root), plan["plan_token"]],
                cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(
                result.returncode, 73,
                (checkpoint, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")),
            )
            state = self.root / "data" / ".helios-room-reset-state.json"
            next_path = Path(f"{state}.next")
            journal_path = next_path if next_path.exists() else state
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertIn(journal["stage"], {
                "ready_to_quarantine", "quarantining", "original_quarantined",
                "installing_fresh", "fresh_installed", "validating_fresh",
                "fresh_validated", "cleaning_quarantine", "finalizing",
            })
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
        original = self.root / "data" / (
            f".helios.db.reset-{plan['plan_token']}.original"
        )
        original.unlink()
        script = (
            "import os,sys\n"
            "from app.reset_database import recover_database_reset\n"
            "def crash(stage):\n"
            "    if stage == 'recovery:backup_copy_partial': os._exit(73)\n"
            "recover_database_reset('data/helios.db', expected_plan_token=sys.argv[2], "
            "action='restore-source', confirm_reset_recovery=True, "
            "repository_root=sys.argv[1], crash_checkpoint=crash)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root), plan["plan_token"]],
            cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
        restoring = self.root / "data" / (
            f".helios.db.reset-{plan['plan_token']}.restoring"
        )
        backup = self.root / plan["backup_path"]
        self.assertGreater(restoring.stat().st_size, 0)
        self.assertLess(restoring.stat().st_size, backup.stat().st_size)
        report = recover_database_reset(
            "data/helios.db", expected_plan_token=plan["plan_token"],
            action="restore-source", confirm_reset_recovery=True,
            repository_root=self.root,
        )
        self.assertEqual(report["status"], "restored_source")
        self.assertFalse(restoring.exists())

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
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
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
                journal = json.loads(
                    (root / "data" / ".helios-room-reset-state.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual((journal["stage"], journal["journal_sequence"]), (stage, sequence))
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


if __name__ == "__main__":
    unittest.main()
