from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import models as genai_models
from google.genai import types

from app.database import (
    DEFAULT_SCHEMA_PATH,
    _ensure_active_membership,
    _ensure_identity_bootstrap,
    _ensure_initial_helios_config,
    _ensure_participant,
    _ensure_room,
    connect_database,
    create_turn,
    initialize_database,
    store_message,
)
from app.gemini_client import (
    GEMINI_LEGACY_MAX_OUTPUT_TOKENS,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_25_THINKING_MAX_OUTPUT_TOKENS,
    GEMINI_SYSTEM_INSTRUCTIONS,
    GEMINI_SYSTEM_INSTRUCTIONS_V2,
    GEMINI_SYSTEM_INSTRUCTIONS_V3,
    GEMINI_THINKING_POLICY_VERSION,
    MAX_KEY_LENGTH,
    MAX_SAFE_INTEGER,
    MAX_STRING_LENGTH,
    content_from_recorded,
    create_gemini_client,
    generate_content_config,
    is_timeout_exception,
    load_gemini_environment,
    model_aware_request_contract,
    model_content_from_stored_response,
    recorded_request_config,
    recorded_settings,
    recorded_settings_from_request_config,
    resolve_gemini_thinking_policy,
    safe_gemini_exception_diagnostics,
    serialize_and_evaluate_response,
    validate_recorded_google_request_payload,
    validate_recorded_google_shared_request_payload,
    validate_stored_success_response,
)
from app.gemini_identity import (
    GeminiIdentityError,
    WELCOME_CONFIG_LABEL,
    _welcome_settings,
    install_gemini,
    publish_gemini_welcome,
)
from app.gemini_service import find_or_create_gemini_configuration, run_gemini_turn
from app.identity_service import current_alias, post_room_message
from app.main import app
from app.provider_history import load_provider_history
from app.room_service import TurnServiceError
from app.room_service import run_helios_turn
from app.seed_memory import import_seed_memories, load_seed_manifest, parse_seed_manifest
from app.trace_service import TraceServiceError, load_trace, project_trace_json


SYNTHETIC_KEY = "synthetic-gemini-key-DO-NOT-USE"


class FakeModels:
    def __init__(self, effect: Any) -> None:
        self.effect = effect
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **request: Any) -> Any:
        self.calls.append(request)
        if isinstance(self.effect, BaseException):
            raise self.effect
        return self.effect


class FakeAio:
    def __init__(self, effect: Any) -> None:
        self.models = FakeModels(effect)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, effect: Any) -> None:
        self.aio = FakeAio(effect)


class RawResponse:
    def __init__(self, raw: dict[str, Any], text: str | None) -> None:
        self.raw = raw
        self.text = text

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        if mode != "python" or exclude_none is not True:
            raise AssertionError("Gemini evidence must be captured in Python mode")
        return self.raw


def _quoted_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _logical_sqlite_value(value: Any) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bytes):
        return ("blob", value.hex())
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        return ("real", value.hex())
    if isinstance(value, str):
        return ("text", value)
    raise AssertionError(f"unsupported SQLite value type: {type(value)!r}")


def _logical_sqlite_rows(rows: Any) -> tuple[tuple[tuple[str, Any], ...], ...]:
    normalized = [
        tuple(_logical_sqlite_value(value) for value in row)
        for row in rows
    ]
    return tuple(sorted(normalized))


def logical_database_snapshot(database_path: Path) -> dict[str, Any]:
    """Capture complete deterministic logical SQLite state using raw SQL only."""

    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        schema_rows = list(connection.execute(
            """SELECT type,name,tbl_name,rootpage,sql
               FROM sqlite_schema
               ORDER BY type,name,tbl_name,rootpage"""
        ))
        user_table_names = [
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_schema
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        tables: list[dict[str, Any]] = []
        for table_name in user_table_names:
            quoted_table = _quoted_sqlite_identifier(table_name)
            index_rows = list(connection.execute(f"PRAGMA index_list({quoted_table})"))
            index_details = []
            for index_row in index_rows:
                index_name = index_row[1]
                quoted_index = _quoted_sqlite_identifier(index_name)
                index_details.append((
                    index_name,
                    _logical_sqlite_rows(
                        connection.execute(f"PRAGMA index_xinfo({quoted_index})")
                    ),
                ))
            tables.append({
                "name": table_name,
                "columns": _logical_sqlite_rows(
                    connection.execute(f"PRAGMA table_xinfo({quoted_table})")
                ),
                "foreign_keys": _logical_sqlite_rows(
                    connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
                ),
                "indexes": _logical_sqlite_rows(index_rows),
                "index_details": tuple(sorted(index_details)),
                "rows": _logical_sqlite_rows(
                    connection.execute(f"SELECT * FROM {quoted_table}")
                ),
            })
        has_sequence = any(
            row[0] == "table" and row[1] == "sqlite_sequence"
            for row in schema_rows
        )
        sequence_rows = (
            _logical_sqlite_rows(
                connection.execute("SELECT name,seq FROM sqlite_sequence")
            )
            if has_sequence
            else ()
        )
        return {
            "pragmas": {
                "application_id": connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0],
                "encoding": connection.execute("PRAGMA encoding").fetchone()[0],
                "user_version": connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0],
            },
            "schema": _logical_sqlite_rows(schema_rows),
            "sequence": sequence_rows,
            "tables": tuple(tables),
        }
    finally:
        connection.close()


def success_response(text: str = "Gemini reply") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                index=0,
                finish_reason="STOP",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text="private thought",
                            thought=True,
                            thought_signature=b"\xfb\xff\x00",
                        ),
                        types.Part(text=text),
                    ],
                ),
            )
        ],
        model_version="gemini-resolved",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=4,
            candidates_token_count=3,
            thoughts_token_count=2,
            total_token_count=9,
        ),
    )


class GeminiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_path = Path(self.temp.name) / "room.db"
        self.dotenv_path = Path(self.temp.name) / "missing.env"
        initialize_database(self.database_path)

    def run_turn(
        self,
        effect: Any,
        message: str = "Hello Gemini",
        *,
        model: str = "gemini-test",
    ) -> tuple[Any, FakeClient]:
        client = FakeClient(effect)
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": SYNTHETIC_KEY,
                "HELIOS_GEMINI_MODEL": model,
                "GOOGLE_API_KEY": "hostile-google-key",
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_GENAI_USE_ENTERPRISE": "true",
                "GOOGLE_CLOUD_PROJECT": "hostile-project",
                "GOOGLE_CLOUD_LOCATION": "hostile-location",
            },
            clear=True,
        ):
            result = asyncio.run(
                run_gemini_turn(
                    message,
                    database_path=self.database_path,
                    client_factory=lambda key: client if key == SYNTHETIC_KEY else None,
                    dotenv_path=self.dotenv_path,
                )
            )
        return result, client

    def test_sdk_local_afc_is_disabled_and_absent_from_wire(self) -> None:
        self.assertNotIn("automatic_function_calling", recorded_request_config())
        client = genai.Client(api_key="synthetic-only", vertexai=False, enterprise=False)
        self.addCleanup(client.close)
        expected = {
            "gemini-3.6-flash": (
                {"includeThoughts": False, "thinkingLevel": "MEDIUM"},
                GEMINI_MAX_OUTPUT_TOKENS,
            ),
            "gemini-2.5-flash-lite": (
                {"includeThoughts": False, "thinkingBudget": 0},
                GEMINI_MAX_OUTPUT_TOKENS,
            ),
            "gemini-2.5-flash": (
                {"includeThoughts": False, "thinkingBudget": 8_192},
                GEMINI_25_THINKING_MAX_OUTPUT_TOKENS,
            ),
            "gemini-3-pro-preview": (None, GEMINI_MAX_OUTPUT_TOKENS),
            "unknown-future-model": (None, GEMINI_MAX_OUTPUT_TOKENS),
        }

        def keys(value: Any) -> set[str]:
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, (list, tuple)):
                return set().union(*(keys(item) for item in value))
            return set()

        for model, (expected_thinking, expected_limit) in expected.items():
            with self.subTest(model=model):
                _policy, recorded_config, settings = model_aware_request_contract(model)
                config = generate_content_config(recorded_config)
                self.assertTrue(config.automatic_function_calling.disable)
                self.assertEqual(config.max_output_tokens, expected_limit)
                parent: dict[str, Any] = {}
                converted = genai_models._GenerateContentConfig_to_mldev(
                    client._api_client, config, parent, config
                )
                wire_keys = keys({"generationConfig": converted, **parent})
                self.assertNotIn("automatic_function_calling", wire_keys)
                self.assertNotIn("automaticFunctionCalling", wire_keys)
                wire_thinking = converted.get("thinkingConfig")
                if expected_thinking is None:
                    self.assertIsNone(wire_thinking)
                    self.assertNotIn("thinking_config", recorded_config)
                    self.assertEqual(settings["thinking"], {"mode": "provider_default"})
                else:
                    self.assertEqual(
                        wire_thinking.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                        expected_thinking,
                    )

    def test_output_limit_versions_new_requests_and_preserves_historical_v4(self) -> None:
        historical_settings_json = json.dumps(
            recorded_settings(max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS),
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(connect_database(self.database_path)) as connection:
            gemini_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='gemini'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO participant_configs (
                       participant_id,provider,model,config_label,
                       system_instructions,settings_json,tools_json
                   ) VALUES (?, 'google', 'gemini-2.5-flash-lite',
                       'direct-address-google-gemini-2-5-flash-lite-v1', ?, ?, '[]')""",
                (gemini_id, GEMINI_SYSTEM_INSTRUCTIONS_V3, historical_settings_json),
            )
            connection.commit()
        cases = (
            ("gemini-3.6-flash", GEMINI_MAX_OUTPUT_TOKENS, {"include_thoughts": False, "thinking_level": "medium"}),
            ("gemini-2.5-flash-lite", GEMINI_MAX_OUTPUT_TOKENS, {"include_thoughts": False, "thinking_budget": 0}),
            ("gemini-2.5-flash", GEMINI_25_THINKING_MAX_OUTPUT_TOKENS, {"include_thoughts": False, "thinking_budget": 8_192}),
            ("unknown-future-model", GEMINI_MAX_OUTPUT_TOKENS, None),
        )
        recorded_payloads: dict[str, dict[str, Any]] = {}
        for model, output_limit, thinking in cases:
            result, client = self.run_turn(
                success_response(f"response from {model}"), model=model
            )
            self.assertEqual(len(client.aio.models.calls), 1)
            with closing(connect_database(self.database_path)) as connection:
                event = connection.execute(
                    "SELECT participant_config_id,payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                    (result["turn_id"],),
                ).fetchone()
                payload = json.loads(event["payload_json"])
                recorded_payloads[model] = payload
                settings = json.loads(
                    connection.execute(
                        "SELECT settings_json FROM participant_configs WHERE id=?",
                        (event["participant_config_id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(
                payload["local_context"]["gemini_thinking_policy_version"],
                GEMINI_THINKING_POLICY_VERSION,
            )
            self.assertEqual(
                payload["local_context"]["history_visibility"][
                    "projection_version"
                ],
                "provider_history_v4",
            )
            self.assertEqual(payload["request"]["config"]["max_output_tokens"], output_limit)
            self.assertEqual(payload["request"]["config"].get("thinking_config"), thinking)
            self.assertEqual(
                settings,
                recorded_settings_from_request_config(payload["request"]["config"]),
            )
            trace = load_trace(self.database_path, result["turn_id"])
            self.assertEqual(trace["turn"]["id"], result["turn_id"])
            self.assertEqual(
                trace["recorded_request"]["request"]["config"].get(
                    "thinking_config"
                ),
                thinking,
            )
            self.assertEqual(trace["configurations"][0]["settings"], settings)

        historical = json.loads(json.dumps(recorded_payloads["gemini-2.5-flash-lite"]))
        del historical["local_context"]["gemini_thinking_policy_version"]
        historical["request"]["config"] = recorded_request_config(
            GEMINI_SYSTEM_INSTRUCTIONS_V3,
            max_output_tokens=GEMINI_LEGACY_MAX_OUTPUT_TOKENS,
        )
        self.assertIs(validate_recorded_google_shared_request_payload(historical), historical)
        turn_37_shape = json.loads(json.dumps(historical))
        turn_37_shape["request"]["config"] = recorded_request_config(
            GEMINI_SYSTEM_INSTRUCTIONS_V3,
            max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(
            turn_37_shape["request"]["model"], "gemini-2.5-flash-lite"
        )
        self.assertNotIn(
            "gemini_thinking_policy_version", turn_37_shape["local_context"]
        )
        self.assertIs(
            validate_recorded_google_shared_request_payload(turn_37_shape),
            turn_37_shape,
        )
        current_hybrid = json.loads(json.dumps(historical))
        current_hybrid["local_context"]["gemini_thinking_policy_version"] = (
            GEMINI_THINKING_POLICY_VERSION
        )
        with self.assertRaises(ValueError):
            validate_recorded_google_shared_request_payload(current_hybrid)

        historical_8192 = json.loads(json.dumps(recorded_payloads["gemini-3.6-flash"]))
        del historical_8192["local_context"]["gemini_thinking_policy_version"]
        self.assertIs(
            validate_recorded_google_shared_request_payload(historical_8192),
            historical_8192,
        )
        unsupported_payload = json.loads(json.dumps(historical_8192))
        unsupported_payload["request"]["config"]["max_output_tokens"] = 4_096
        with self.assertRaises(ValueError):
            validate_recorded_google_shared_request_payload(unsupported_payload)
        with closing(connect_database(self.database_path)) as connection:
            flash_lite_rows = connection.execute(
                """SELECT config_label,settings_json FROM participant_configs
                   WHERE model='gemini-2.5-flash-lite' ORDER BY id"""
            ).fetchall()
        self.assertEqual(
            [row["config_label"] for row in flash_lite_rows],
            [
                "direct-address-google-gemini-2-5-flash-lite-v1",
                "direct-address-google-gemini-2-5-flash-lite-v2",
            ],
        )
        self.assertEqual(flash_lite_rows[0]["settings_json"], historical_settings_json)
        self.assertEqual(
            json.loads(flash_lite_rows[1]["settings_json"])["thinking"],
            {"include_thoughts": False, "budget": 0},
        )

    def test_model_classification_is_closed_and_rejects_current_hybrids(self) -> None:
        self.assertEqual(
            resolve_gemini_thinking_policy("gemini-3.6-flash").mode,
            "thinking_level",
        )
        self.assertEqual(
            resolve_gemini_thinking_policy("gemini-2.5-flash-lite").budget,
            0,
        )
        self.assertEqual(
            resolve_gemini_thinking_policy("gemini-2.5-flash").budget,
            8_192,
        )
        for model in (
            "gemini-3-pro-preview",
            "gemini-2.5-flash-lite-preview",
            "Gemini-2.5-Flash-Lite",
            "gemini-4-flash",
            "contains-gemini-3.6-flash",
        ):
            with self.subTest(model=model):
                policy, request, settings = model_aware_request_contract(model)
                self.assertEqual(policy.mode, "provider_default")
                self.assertNotIn("thinking_config", request)
                self.assertEqual(settings["thinking"], {"mode": "provider_default"})
        for invalid in ("", " gemini-3.6-flash", "gemini-3.6-flash "):
            with self.assertRaises(ValueError):
                resolve_gemini_thinking_policy(invalid)

        result, _client = self.run_turn(
            success_response("hybrid source"), model="gemini-3.6-flash"
        )
        with closing(connect_database(self.database_path)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                    (result["turn_id"],),
                ).fetchone()[0]
            )
        both = json.loads(json.dumps(payload))
        both["request"]["config"]["thinking_config"]["thinking_budget"] = 8_192
        with self.assertRaises(ValueError):
            validate_recorded_google_shared_request_payload(both)
        wrong_family = json.loads(json.dumps(payload))
        wrong_family["request"]["model"] = "gemini-2.5-flash-lite"
        with self.assertRaises(ValueError):
            validate_recorded_google_shared_request_payload(wrong_family)
        unknown_marker = json.loads(json.dumps(payload))
        unknown_marker["local_context"]["gemini_thinking_policy_version"] = "future"
        with self.assertRaises(ValueError):
            validate_recorded_google_shared_request_payload(unknown_marker)

    def test_model_aware_v1_unknown_trace_uses_frozen_policy(self) -> None:
        model = "gemini-4-flash"
        result, _client = self.run_turn(
            success_response("future model response"), model=model
        )
        with closing(connect_database(self.database_path)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM api_events WHERE turn_id=? AND sequence_no=1",
                    (result["turn_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(
            payload["local_context"]["gemini_thinking_policy_version"],
            "model_aware_v1",
        )
        self.assertNotIn("thinking_config", payload["request"]["config"])

        future_medium_contract = model_aware_request_contract("gemini-3.6-flash")
        with (
            patch(
                "app.gemini_client.GEMINI_THINKING_POLICY_VERSION",
                "model_aware_v2",
            ),
            patch(
                "app.gemini_client.model_aware_request_contract",
                return_value=future_medium_contract,
            ) as future_current_contract,
        ):
            trace = load_trace(self.database_path, result["turn_id"])

        future_current_contract.assert_not_called()
        self.assertEqual(trace["turn"]["id"], result["turn_id"])
        self.assertEqual(
            trace["recorded_request"]["local_context"][
                "gemini_thinking_policy_version"
            ],
            "model_aware_v1",
        )
        self.assertNotIn(
            "thinking_config", trace["recorded_request"]["request"]["config"]
        )

    def test_safe_api_error_evidence_and_turn_37_regression(self) -> None:
        provider_error = genai_errors.ClientError(
            400,
            {
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "Thinking level is not supported for this model.",
                    "details": [{"headers": {"Authorization": SYNTHETIC_KEY}}],
                }
            },
        )
        expected = {
            "error_class": "ProviderError",
            "reason": "gemini_provider_failure",
            "summary": "The Gemini provider request failed.",
            "http_status": 400,
            "provider_status": "INVALID_ARGUMENT",
            "provider_message": "Thinking level is not supported for this model.",
        }
        self.assertEqual(
            safe_gemini_exception_diagnostics(
                provider_error, timeout=False, api_key=SYNTHETIC_KEY
            ),
            expected,
        )
        with self.assertRaises(TurnServiceError) as caught:
            self.run_turn(provider_error, model="gemini-2.5-flash-lite")
        self.assertEqual(caught.exception.code, "gemini_provider_failure")
        with closing(connect_database(self.database_path)) as connection:
            terminal = json.loads(
                connection.execute(
                    "SELECT payload_json FROM api_events WHERE sequence_no=2"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM messages WHERE participant_config_id IS NOT NULL"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(terminal, {"error": expected})
        serialized = json.dumps(terminal)
        self.assertNotIn(SYNTHETIC_KEY, serialized)
        self.assertNotIn("Authorization", serialized)
        trace = load_trace(self.database_path, caught.exception.turn_id)
        self.assertEqual(
            trace["api_events"][-1]["payload"]["error"]["provider_status"],
            "INVALID_ARGUMENT",
        )

        generic = {
            "error_class": "ProviderError",
            "reason": "gemini_provider_failure",
            "summary": "The Gemini provider request failed.",
        }
        for message in (
            f"Authorization: Bearer {SYNTHETIC_KEY}",
            f"api_key={SYNTHETIC_KEY}",
            "unsafe\ncontrol",
            "<script>alert(1)</script>",
            "x" * 513,
            "\u200bhidden-format",
            "\ud800",
        ):
            hostile = genai_errors.ClientError(
                400,
                {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}},
            )
            diagnostics = safe_gemini_exception_diagnostics(
                hostile, timeout=False, api_key=SYNTHETIC_KEY
            )
            self.assertNotIn("provider_message", diagnostics)
            self.assertNotIn(SYNTHETIC_KEY, json.dumps(diagnostics))

        class ExtractionFailure(genai_errors.APIError):
            @property
            def code(self) -> int:
                raise RuntimeError("must be swallowed")

        extraction_failure = Exception.__new__(ExtractionFailure)
        Exception.__init__(extraction_failure, "unsafe repr")
        self.assertEqual(
            safe_gemini_exception_diagnostics(
                extraction_failure, timeout=False, api_key=SYNTHETIC_KEY
            ),
            generic,
        )
        self.assertEqual(
            safe_gemini_exception_diagnostics(
                RuntimeError("status=400"), timeout=False, api_key=SYNTHETIC_KEY
            ),
            generic,
        )

        accessed: list[str] = []

        class ExactBoundary(genai_errors.APIError):
            @property
            def code(self) -> int:
                accessed.append("code")
                return 400

            @property
            def status(self) -> str:
                accessed.append("status")
                return "INVALID_ARGUMENT"

            @property
            def message(self) -> str:
                accessed.append("message")
                return "Safe provider message."

            @property
            def details(self) -> Any:
                raise AssertionError("details must not be accessed")

            @property
            def response(self) -> Any:
                raise AssertionError("response must not be accessed")

        boundary_error = Exception.__new__(ExactBoundary)
        Exception.__init__(boundary_error, "repr must not be used")
        boundary_diagnostics = safe_gemini_exception_diagnostics(
            boundary_error, timeout=False, api_key=SYNTHETIC_KEY
        )
        self.assertEqual(accessed, ["code", "status", "message"])
        self.assertEqual(
            {
                key: boundary_diagnostics[key]
                for key in ("http_status", "provider_status", "provider_message")
            },
            {
                "http_status": 400,
                "provider_status": "INVALID_ARGUMENT",
                "provider_message": "Safe provider message.",
            },
        )

    def test_client_uses_explicit_developer_backend_under_hostile_environment(self) -> None:
        captured: dict[str, Any] = {}

        class Constructor:
            def __new__(cls, **kwargs: Any) -> object:
                captured.update(kwargs)
                return object()

        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "hostile",
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_GENAI_USE_ENTERPRISE": "true",
                "GOOGLE_CLOUD_PROJECT": "hostile",
                "GOOGLE_CLOUD_LOCATION": "hostile",
            },
            clear=True,
        ):
            create_gemini_client(SYNTHETIC_KEY, client_class=Constructor)
        self.assertEqual(captured["api_key"], SYNTHETIC_KEY)
        self.assertIs(captured["vertexai"], False)
        self.assertIs(captured["enterprise"], False)
        self.assertEqual(captured["http_options"].api_version, "v1beta")
        self.assertEqual(captured["http_options"].timeout, 120_000)
        self.assertEqual(captured["http_options"].retry_options.attempts, 1)

    def test_success_records_exact_google_turn_and_signature_round_trip(self) -> None:
        result, client = self.run_turn(success_response("  exact reply  "))
        self.assertEqual(set(result), {"gemini_message_id", "peter_message_id", "status", "turn_id"})
        self.assertEqual(len(client.aio.models.calls), 1)
        self.assertTrue(client.aio.closed)
        call = client.aio.models.calls[0]
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["config"].system_instruction, GEMINI_SYSTEM_INSTRUCTIONS_V3)
        self.assertTrue(call["config"].automatic_function_calling.disable)

        with closing(connect_database(self.database_path)) as connection:
            events = connection.execute(
                "SELECT sequence_no,event_type,related_message_id,payload_json FROM api_events ORDER BY sequence_no"
            ).fetchall()
            self.assertEqual([row["event_type"] for row in events], [
                "google.generate_content.request", "google.generate_content.response"
            ])
            request = json.loads(events[0]["payload_json"])
            self.assertEqual(request["local_context"]["sdk_policy"], {
                "automatic_function_calling": {"disable": True}
            })
            self.assertNotIn("automatic_function_calling", request["request"]["config"])
            response = json.loads(events[1]["payload_json"])["response"]
            signed = response["candidates"][0]["content"]["parts"][0]
            self.assertEqual(signed["thought_signature_b64"], "+/8A")
            reconstructed = content_from_recorded(response["candidates"][0]["content"])
            self.assertEqual(reconstructed.parts[0].thought_signature, b"\xfb\xff\x00")
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "completed")
            self.assertEqual(connection.execute(
                "SELECT message_text FROM messages WHERE participant_id=(SELECT id FROM participants WHERE participant_key='gemini')"
            ).fetchone()[0], "  exact reply  ")

    def test_failure_envelopes_use_fixed_classes_and_preserve_peter_message(self) -> None:
        class HostileProviderSubclass(RuntimeError):
            status_code = 429
            code = "quota_exceeded"
            request_id = "request-1"

        with self.assertRaises(TurnServiceError) as caught:
            self.run_turn(HostileProviderSubclass("unrestricted secret text"))
        self.assertEqual(caught.exception.code, "gemini_provider_failure")
        with closing(connect_database(self.database_path)) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM api_events WHERE sequence_no=2"
            ).fetchone()[0])
            self.assertEqual(payload["error"]["error_class"], "ProviderError")
            self.assertNotIn("HostileProviderSubclass", json.dumps(payload))
            self.assertNotIn("unrestricted", json.dumps(payload))
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "failed")

    def test_google_terminal_error_and_trace_matrix_is_exact_and_sanitized(self) -> None:
        class HostileProviderFailure(RuntimeError):
            status_code = 429
            code = "<invalid-provider-code>"
            request_id = SYNTHETIC_KEY

        rejected_text = '<script>PRIVATE_UNUSABLE_OUTPUT</script>'
        unusable_raw = {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [{"text": rejected_text}],
                    "future~/extension": {
                        "client_secret": "PRIVATE_EXTENSION_DESCENDANT"
                    },
                },
                "finish_reason": "MAX_TOKENS",
                "index": 0,
                "future_candidate": "PRIVATE_CANDIDATE_EXTENSION",
            }],
            "model_version": "resolved-model",
            "usage_metadata": {
                "prompt_token_count": 2,
                "candidates_token_count": 1,
                "total_token_count": 3,
            },
            "future_top": {"private": "PRIVATE_TOP_EXTENSION"},
        }
        cases = (
            {
                "name": "provider_exception",
                "effect": HostileProviderFailure(
                    '<img src=x onerror="PRIVATE_PROVIDER_EXCEPTION">'
                ),
                "status_code": 502,
                "public_error": "gemini_provider_failure",
                "public_message": "The Gemini provider request failed.",
                "terminal": {
                    "error": {
                        "error_class": "ProviderError",
                        "reason": "gemini_provider_failure",
                        "summary": "The Gemini provider request failed.",
                    }
                },
                "private_values": (
                    "PRIVATE_PROVIDER_EXCEPTION",
                    "<invalid-provider-code>",
                    SYNTHETIC_KEY,
                ),
            },
            {
                "name": "timeout",
                "effect": httpx.ReadTimeout(
                    '<script>PRIVATE_TIMEOUT_EXCEPTION</script>'
                ),
                "status_code": 504,
                "public_error": "gemini_provider_timeout",
                "public_message": "The Gemini provider request timed out.",
                "terminal": {
                    "error": {
                        "error_class": "TimeoutError",
                        "reason": "gemini_provider_timeout",
                        "summary": "The Gemini provider request timed out.",
                    }
                },
                "private_values": ("PRIVATE_TIMEOUT_EXCEPTION",),
            },
            {
                "name": "serialization_failure",
                "effect": RawResponse(
                    {
                        "candidates": [{
                            "content": {
                                "role": "model",
                                "parts": [{"text": SYNTHETIC_KEY}],
                            },
                            "finish_reason": "STOP",
                            "index": 0,
                        }]
                    },
                    SYNTHETIC_KEY,
                ),
                "status_code": 502,
                "public_error": "gemini_provider_response_serialization_failed",
                "public_message": "The Gemini provider response could not be recorded safely.",
                "terminal": {
                    "error": {
                        "error_class": "ResponseSerializationError",
                        "reason": "gemini_provider_response_serialization_failed",
                        "summary": "The Gemini provider response could not be recorded safely.",
                    }
                },
                "private_values": (SYNTHETIC_KEY,),
            },
            {
                "name": "unusable_response",
                "effect": RawResponse(unusable_raw, rejected_text),
                "status_code": 502,
                "public_error": "gemini_provider_unusable_response",
                "public_message": "The Gemini provider returned an unusable response.",
                "terminal": {
                    "error": {
                        "failure_kind": "invalid_finish_reason",
                        "reason": "gemini_provider_unusable_response",
                        "summary": "The Gemini provider returned an unusable response.",
                    },
                    "response": unusable_raw,
                },
                "private_values": (
                    rejected_text,
                    "PRIVATE_EXTENSION_DESCENDANT",
                    "PRIVATE_CANDIDATE_EXTENSION",
                    "PRIVATE_TOP_EXTENSION",
                ),
            },
        )

        old_state = (
            app.state.database_path,
            app.state.gemini_client_factory,
            app.state.dotenv_path,
        )
        self.addCleanup(setattr, app.state, "database_path", old_state[0])
        self.addCleanup(setattr, app.state, "gemini_client_factory", old_state[1])
        self.addCleanup(setattr, app.state, "dotenv_path", old_state[2])

        async def post() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as session:
                return await session.post(
                    "/api/messages",
                    json={
                        "message_text": "exact failed trigger",
                        "destination": {
                            "kind": "participant",
                            "participant_key": "gemini",
                        },
                    },
                )

        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                database_path = Path(self.temp.name) / f"terminal-{index}.db"
                initialize_database(database_path)
                client = FakeClient(case["effect"])
                factory_calls: list[str] = []

                def factory(key: str) -> FakeClient:
                    factory_calls.append(key)
                    return client

                app.state.database_path = database_path
                app.state.gemini_client_factory = factory
                app.state.dotenv_path = self.dotenv_path
                with patch.dict(
                    os.environ,
                    {
                        "GEMINI_API_KEY": SYNTHETIC_KEY,
                        "HELIOS_GEMINI_MODEL": "gemini-test",
                    },
                    clear=True,
                ):
                    response = asyncio.run(post())

                expected_public = {
                    "error": case["public_error"],
                    "message": case["public_message"],
                    "peter_message_id": 1,
                    "turn_id": 1,
                }
                self.assertEqual(response.status_code, case["status_code"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json(), expected_public)
                self.assertEqual(factory_calls, [SYNTHETIC_KEY])
                self.assertEqual(len(client.aio.models.calls), 1)

                with closing(connect_database(database_path)) as connection:
                    turn = connection.execute(
                        "SELECT id,room_id,status,initiated_by_participant_id FROM turns"
                    ).fetchone()
                    messages = connection.execute(
                        """SELECT m.id,m.turn_id,m.participant_id,m.participant_config_id,
                                  m.message_text,p.participant_key
                           FROM messages AS m
                           JOIN participants AS p ON p.id=m.participant_id
                           ORDER BY m.id"""
                    ).fetchall()
                    events = connection.execute(
                        """SELECT ae.sequence_no,ae.event_type,ae.turn_id,ae.room_id,
                                  ae.participant_id,ae.participant_config_id,
                                  ae.related_message_id,ae.payload_json,p.participant_key,
                                  pc.participant_id AS configuration_owner
                           FROM api_events AS ae
                           JOIN participants AS p ON p.id=ae.participant_id
                           JOIN participant_configs AS pc ON pc.id=ae.participant_config_id
                           ORDER BY ae.sequence_no"""
                    ).fetchall()
                    self.assertEqual(turn["status"], "failed")
                    self.assertEqual(len(messages), 1)
                    self.assertEqual(messages[0]["participant_key"], "peter")
                    self.assertEqual(
                        turn["initiated_by_participant_id"], messages[0]["participant_id"]
                    )
                    self.assertEqual(messages[0]["message_text"], "exact failed trigger")
                    self.assertIsNone(messages[0]["participant_config_id"])
                    self.assertEqual(len(events), 2)
                    self.assertEqual(
                        [(event["sequence_no"], event["event_type"]) for event in events],
                        [
                            (1, "google.generate_content.request"),
                            (2, "google.generate_content.error"),
                        ],
                    )
                    for event in events:
                        self.assertEqual(event["turn_id"], turn["id"])
                        self.assertEqual(event["room_id"], turn["room_id"])
                        self.assertEqual(event["participant_key"], "gemini")
                        self.assertEqual(
                            event["participant_id"], event["configuration_owner"]
                        )
                        self.assertEqual(
                            event["participant_config_id"],
                            events[0]["participant_config_id"],
                        )
                        self.assertEqual(
                            event["related_message_id"], messages[0]["id"]
                        )
                    request_payload = json.loads(events[0]["payload_json"])
                    self.assertEqual(set(request_payload), {"local_context", "request"})
                    self.assertEqual(request_payload["request"]["model"], "gemini-test")
                    self.assertEqual(
                        request_payload["request"]["contents"][-1],
                        {
                            "role": "user",
                            "parts": [{"text": "exact failed trigger"}],
                        },
                    )
                    terminal_payload = json.loads(events[1]["payload_json"])
                    self.assertEqual(terminal_payload, case["terminal"])
                    if case["name"] == "provider_exception":
                        self.assertNotIn("provider_error_code", terminal_payload["error"])
                        self.assertNotIn("provider_request_id", terminal_payload["error"])
                    self.assertEqual(
                        connection.execute(
                            """SELECT count(*) FROM messages AS m
                               JOIN participants AS p ON p.id=m.participant_id
                               WHERE p.participant_key='gemini'"""
                        ).fetchone()[0],
                        0,
                    )

                trace = load_trace(database_path, 1)
                self.assertEqual(trace["turn"]["status"], "failed")
                self.assertEqual(len(trace["messages"]), 1)
                self.assertEqual(
                    [event["event_type"] for event in trace["api_events"]],
                    ["google.generate_content.request", "google.generate_content.error"],
                )
                terminal_trace = trace["api_events"][1]
                pointers = terminal_trace["omitted_json_pointers"]
                self.assertEqual(pointers, sorted(set(pointers)))
                if case["name"] == "unusable_response":
                    expected_pointers = [
                        "/response/candidates/0/content/future~0~1extension",
                        "/response/candidates/0/content/parts/0/text",
                        "/response/candidates/0/future_candidate",
                        "/response/future_top",
                    ]
                    self.assertEqual(pointers, expected_pointers)
                    expected_trace_payload = {
                        "error": case["terminal"]["error"],
                        "response": {
                            "candidates": [{
                                "content": {"role": "model", "parts": [{}]},
                                "finish_reason": "MAX_TOKENS",
                                "index": 0,
                            }],
                            "model_version": "resolved-model",
                            "usage_metadata": {
                                "prompt_token_count": 2,
                                "candidates_token_count": 1,
                                "total_token_count": 3,
                            },
                        },
                    }
                else:
                    self.assertEqual(pointers, [])
                    expected_trace_payload = case["terminal"]
                self.assertEqual(terminal_trace["payload"], expected_trace_payload)
                serialized_public = json.dumps(response.json())
                serialized_trace = json.dumps(trace)
                for private_value in case["private_values"]:
                    self.assertNotIn(private_value, serialized_public)
                    self.assertNotIn(private_value, serialized_trace)

    def test_serialization_and_unusable_failures_have_exact_shapes(self) -> None:
        leaking = RawResponse(
            {
                "candidates": [{
                    "content": {"role": "model", "parts": [{"text": SYNTHETIC_KEY}]},
                    "finish_reason": "STOP", "index": 0,
                }]
            },
            SYNTHETIC_KEY,
        )
        with self.assertRaises(TurnServiceError) as caught:
            self.run_turn(leaking)
        self.assertEqual(caught.exception.code, "gemini_provider_response_serialization_failed")
        with closing(connect_database(self.database_path)) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM api_events WHERE sequence_no=2"
            ).fetchone()[0])
            self.assertEqual(payload, {"error": {
                "error_class": "ResponseSerializationError",
                "reason": "gemini_provider_response_serialization_failed",
                "summary": "The Gemini provider response could not be recorded safely.",
            }})
            self.assertNotIn(SYNTHETIC_KEY, json.dumps(payload))

        second = Path(self.temp.name) / "unusable.db"
        initialize_database(second)
        self.database_path = second
        blocked = RawResponse(
            {
                "candidates": [],
                "prompt_feedback": {"block_reason": "SAFETY", "block_reason_message": "private"},
            },
            None,
        )
        with self.assertRaises(TurnServiceError) as caught:
            self.run_turn(blocked)
        self.assertEqual(caught.exception.code, "gemini_provider_unusable_response")
        with closing(connect_database(second)) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM api_events WHERE sequence_no=2"
            ).fetchone()[0])
            self.assertEqual(payload["error"]["failure_kind"], "invalid_candidate_count")
            self.assertEqual(set(payload), {"error", "response"})

    def test_bounded_extensions_stay_in_database_and_are_omitted_from_trace(self) -> None:
        future_extension = {
            "private_descendant": "content extension descendant value",
            "nested": {"never_visit": "nested content extension value"},
        }
        escaped_extension = {
            "private/value~": "escaped content extension value",
        }
        raw = {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "private thought",
                            "thought": True,
                            "thought_signature": b"\xfb\xff\x00",
                        },
                        {"text": "safe reply"},
                    ],
                    "future_extension": future_extension,
                    "path~/extension": escaped_extension,
                    "z_extension": ["another private extension value"],
                },
                "finish_reason": "STOP", "index": 0,
                "safety_ratings": [{
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "probability": "NEGLIGIBLE",
                    "blocked": False,
                }],
                "future_detail": "<b>private candidate extension</b>",
            }],
            "model_version": "resolved",
            "future/field": {"nested~secret": "<script>attacker()</script>"},
            "usage_metadata": {
                "prompt_token_count": 1,
                "candidates_token_count": 1,
                "total_token_count": 2,
                "prompt_tokens_details": [{"modality": "TEXT", "token_count": 1}],
                "traffic_type": "ON_DEMAND",
            },
            "model_status": {"message": "unknown future detail"},
            "x-goog-api-key": "stored-unrelated-credential",
            "automatic_function_calling_history": [],
        }
        result, first_client = self.run_turn(RawResponse(raw, "safe reply"))
        self.assertEqual(len(first_client.aio.models.calls), 1)
        with closing(connect_database(self.database_path)) as connection:
            stored_payload = json.loads(connection.execute(
                "SELECT payload_json FROM api_events WHERE sequence_no=2"
            ).fetchone()[0])
            stored_response = stored_payload["response"]
            stored_content = stored_response["candidates"][0]["content"]
            self.assertEqual(stored_content["future_extension"], future_extension)
            self.assertEqual(stored_content["path~/extension"], escaped_extension)
            self.assertEqual(
                stored_content["z_extension"],
                ["another private extension value"],
            )
            self.assertEqual(
                stored_content["parts"][0]["thought_signature_b64"], "+/8A"
            )
            canonical = model_content_from_stored_response(stored_response)
            self.assertEqual(set(canonical), {"role", "parts"})
            self.assertEqual(canonical["role"], "model")
            self.assertEqual(canonical["parts"], stored_content["parts"])
            self.assertIsNot(canonical, stored_content)
            self.assertIsNot(canonical["parts"], stored_content["parts"])
            self.assertIn("<script>attacker()", json.dumps(stored_payload))
        trace = load_trace(self.database_path, result["turn_id"])
        event = trace["api_events"][1]
        rendered_payload = json.dumps(event["payload"])
        for private_value in (
            "attacker",
            "unknown future detail",
            "private candidate extension",
            "stored-unrelated-credential",
            "content extension descendant value",
            "nested content extension value",
            "escaped content extension value",
            "another private extension value",
        ):
            self.assertNotIn(private_value, rendered_payload)
        self.assertNotIn("future_extension", rendered_payload)
        self.assertNotIn("path~/extension", rendered_payload)
        self.assertNotIn("z_extension", rendered_payload)
        pointers = event["omitted_json_pointers"]
        self.assertEqual(pointers, sorted(set(pointers)))
        expected_pointers = {
            "/response/candidates/0/content/future_extension",
            "/response/candidates/0/content/path~0~1extension",
            "/response/candidates/0/content/z_extension",
            "/response/candidates/0/future_detail",
            "/response/future~1field",
            "/response/model_status",
            "/response/usage_metadata/prompt_tokens_details",
            "/response/usage_metadata/traffic_type",
            "/response/x-goog-api-key",
        }
        self.assertTrue(expected_pointers.issubset(pointers))
        for root in (
            "/response/candidates/0/content/future_extension",
            "/response/candidates/0/content/path~0~1extension",
            "/response/candidates/0/content/z_extension",
        ):
            self.assertFalse(any(pointer.startswith(root + "/") for pointer in pointers))

        _second_result, second_client = self.run_turn(
            success_response("second reply"), "second trigger"
        )
        self.assertEqual(len(second_client.aio.models.calls), 1)
        replayed = second_client.aio.models.calls[0]["contents"]
        replayed_model = [content for content in replayed if content.role == "model"]
        self.assertEqual(len(replayed_model), 1)
        self.assertEqual(
            [part.text for part in replayed_model[0].parts],
            ["private thought", "safe reply"],
        )
        self.assertEqual(
            replayed_model[0].parts[0].thought_signature,
            b"\xfb\xff\x00",
        )

    def test_malformed_and_over_bound_content_extensions_fail_serialization(self) -> None:
        def response_with_extension(key: str, value: Any) -> RawResponse:
            return RawResponse(
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [{"text": "visible"}],
                            key: value,
                        },
                        "finish_reason": "STOP",
                        "index": 0,
                    }]
                },
                "visible",
            )

        cases = (
            ("malformed_value", "extension", object()),
            ("over_bound_key", "x" * (MAX_KEY_LENGTH + 1), "private"),
            ("over_bound_string", "extension", "x" * (MAX_STRING_LENGTH + 1)),
        )
        for name, key, value in cases:
            with self.subTest(name=name), self.assertRaises(Exception):
                serialize_and_evaluate_response(
                    response_with_extension(key, value),
                    api_key=SYNTHETIC_KEY,
                )

    def test_unusable_trace_omits_rejected_model_output(self) -> None:
        rejected_text = "<script>rejected private model output</script>"
        unusable = RawResponse(
            {
                "candidates": [{
                    "content": {"role": "model", "parts": [{"text": rejected_text}]},
                    "finish_reason": "MAX_TOKENS",
                    "index": 0,
                }],
                "model_version": "resolved",
            },
            rejected_text,
        )
        with self.assertRaises(TurnServiceError) as caught:
            self.run_turn(unusable)
        trace = load_trace(self.database_path, caught.exception.turn_id)
        rendered = json.dumps(trace)
        self.assertNotIn(rejected_text, rendered)
        self.assertIn(
            "/response/candidates/0/content/parts/0/text",
            trace["api_events"][1]["omitted_json_pointers"],
        )

    def test_api_dispatches_only_gemini_fake_and_uses_safe_http_contract(self) -> None:
        client = FakeClient(success_response())
        old = (
            app.state.database_path,
            app.state.gemini_client_factory,
            app.state.dotenv_path,
        )
        app.state.database_path = self.database_path
        app.state.gemini_client_factory = lambda key: client
        app.state.dotenv_path = self.dotenv_path
        self.addCleanup(setattr, app.state, "database_path", old[0])
        self.addCleanup(setattr, app.state, "gemini_client_factory", old[1])
        self.addCleanup(setattr, app.state, "dotenv_path", old[2])
        transport = httpx.ASGITransport(app=app)

        async def request() -> httpx.Response:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
                return await session.post("/api/messages", json={
                    "message_text": "Hello",
                    "destination": {"kind": "participant", "participant_key": "gemini"},
                })

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"
        }, clear=True):
            response = asyncio.run(request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("gemini_message_id", response.json())

    def test_installer_and_welcome_are_transactional_provider_free_and_idempotent(self) -> None:
        existing = Path(self.temp.name) / "existing.db"
        initialize_database(existing)
        watched_environment = {
            "GEMINI_API_KEY",
            "HELIOS_GEMINI_MODEL",
            "GOOGLE_API_KEY",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        }
        synthetic_environment_source = {
            "GEMINI_API_KEY": "SYNTHETIC_GEMINI_API_KEY_SENTINEL",
            "HELIOS_GEMINI_MODEL": "SYNTHETIC_GEMINI_MODEL_SENTINEL",
            "GOOGLE_API_KEY": "SYNTHETIC_GOOGLE_API_KEY_SENTINEL",
            "GOOGLE_GENAI_USE_VERTEXAI": "SYNTHETIC_VERTEX_BACKEND_SENTINEL",
            "GOOGLE_GENAI_USE_ENTERPRISE": "SYNTHETIC_ENTERPRISE_BACKEND_SENTINEL",
            "GOOGLE_CLOUD_PROJECT": "SYNTHETIC_GOOGLE_PROJECT_SENTINEL",
            "GOOGLE_CLOUD_LOCATION": "SYNTHETIC_GOOGLE_LOCATION_SENTINEL",
        }
        expected_synthetic_source = (
            ("GEMINI_API_KEY", "SYNTHETIC_GEMINI_API_KEY_SENTINEL"),
            ("GOOGLE_API_KEY", "SYNTHETIC_GOOGLE_API_KEY_SENTINEL"),
            ("GOOGLE_CLOUD_LOCATION", "SYNTHETIC_GOOGLE_LOCATION_SENTINEL"),
            ("GOOGLE_CLOUD_PROJECT", "SYNTHETIC_GOOGLE_PROJECT_SENTINEL"),
            ("GOOGLE_GENAI_USE_ENTERPRISE", "SYNTHETIC_ENTERPRISE_BACKEND_SENTINEL"),
            ("GOOGLE_GENAI_USE_VERTEXAI", "SYNTHETIC_VERTEX_BACKEND_SENTINEL"),
            ("HELIOS_GEMINI_MODEL", "SYNTHETIC_GEMINI_MODEL_SENTINEL"),
        )

        class GuardedEnvironment(dict[str, str]):
            enumeration_operations = ("copy", "items", "iter", "keys", "values")

            def __init__(self, source: dict[str, str]) -> None:
                self.synthetic_source = tuple(sorted(source.items()))
                self.access_attempts: list[tuple[str, str]] = []
                self.protected_read_counts = {
                    key: 0 for key in sorted(watched_environment)
                }
                self.enumeration_counts = {
                    operation: 0 for operation in self.enumeration_operations
                }
                super().__init__(source)

            def _guard(self, operation: str, key: object) -> None:
                if key in watched_environment:
                    normalized_key = str(key)
                    self.access_attempts.append((operation, normalized_key))
                    self.protected_read_counts[normalized_key] += 1
                    raise AssertionError(
                        f"protected environment read via {operation}: {normalized_key}"
                    )

            def _reject_enumeration(self, operation: str) -> None:
                self.enumeration_counts[operation] += 1
                for key in sorted(watched_environment):
                    self.access_attempts.append((operation, key))
                    self.protected_read_counts[key] += 1
                raise AssertionError(f"protected environment enumeration via {operation}")

            def __getitem__(self, key: str) -> str:
                self._guard("getitem", key)
                return super().__getitem__(key)

            def get(self, key: str, default: Any = None) -> Any:
                self._guard("get", key)
                return super().get(key, default)

            def __contains__(self, key: object) -> bool:
                self._guard("contains", key)
                return super().__contains__(key)

            def __iter__(self) -> Any:
                self._reject_enumeration("iter")

            def keys(self) -> Any:
                self._reject_enumeration("keys")

            def items(self) -> Any:
                self._reject_enumeration("items")

            def values(self) -> Any:
                self._reject_enumeration("values")

            def copy(self) -> Any:
                self._reject_enumeration("copy")

        def fail_fast(label: str) -> Any:
            def reject(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError(label)

            return reject

        fail_fast_probe = fail_fast("fail-fast boundary self-test")
        with self.assertRaisesRegex(AssertionError, "fail-fast boundary self-test"):
            fail_fast_probe()

        guarded_environment = GuardedEnvironment(synthetic_environment_source)
        self.assertEqual(guarded_environment.synthetic_source, expected_synthetic_source)
        self.assertEqual(
            {value for _key, value in guarded_environment.synthetic_source},
            {value for _key, value in expected_synthetic_source},
        )
        self.assertTrue(all(
            value.startswith("SYNTHETIC_")
            for _key, value in guarded_environment.synthetic_source
        ))
        with patch.object(os, "environ", guarded_environment), patch(
            "app.gemini_client.genai.Client",
            side_effect=fail_fast("Google SDK client construction"),
        ) as sdk_constructor, patch(
            "app.gemini_service.create_gemini_client",
            side_effect=fail_fast("Gemini consumer client construction"),
        ) as service_constructor, patch(
            "app.gemini_service.load_gemini_environment",
            side_effect=fail_fast("Gemini environment load"),
        ) as environment_loader, patch(
            "app.gemini_client.load_dotenv",
            side_effect=fail_fast("dotenv load"),
        ) as dotenv_loader, patch(
            "app.gemini_service.create_gemini_response",
            side_effect=fail_fast("provider invocation"),
        ) as provider_call, patch(
            "socket.create_connection",
            side_effect=fail_fast("network connection"),
        ) as network_connection, patch.object(
            socket.socket,
            "connect",
            side_effect=fail_fast("socket connection"),
        ) as socket_connection:
            self.assertEqual(install_gemini(existing)["status"], "already_installed")
            before = existing.read_bytes()
            self.assertEqual(install_gemini(existing)["status"], "already_installed")
            self.assertEqual(existing.read_bytes(), before)
            published = publish_gemini_welcome(existing)
            self.assertEqual(published["status"], "published")
            second = publish_gemini_welcome(existing)
            self.assertEqual(second["status"], "already_published")
            self.assertEqual(second["message_id"], published["message_id"])
        self.assertEqual(guarded_environment.access_attempts, [])
        self.assertEqual(
            guarded_environment.protected_read_counts,
            {key: 0 for key in sorted(watched_environment)},
        )
        self.assertEqual(
            guarded_environment.enumeration_counts,
            {operation: 0 for operation in GuardedEnvironment.enumeration_operations},
        )
        for dependency in (
            sdk_constructor,
            service_constructor,
            environment_loader,
            dotenv_loader,
            provider_call,
            network_connection,
            socket_connection,
        ):
            self.assertEqual(dependency.call_count, 0)
        trace = load_trace(existing, 1)
        self.assertEqual(trace["api_events"], [])
        self.assertEqual(trace["messages"][0]["routing"]["destination"]["participant_key"], "gemini")

    def test_participant_scoped_import_reports_owner_and_rejects_cross_owner_hash_reuse(self) -> None:
        manifest = load_seed_manifest(
            Path(__file__).resolve().parent.parent / "examples" / "seed-memory-manifest-v1.example.json"
        )
        report = import_seed_memories(
            self.database_path, manifest, owner_participant_key="gemini"
        )
        self.assertEqual(report["owner_participant_key"], "gemini")
        with self.assertRaises(Exception) as caught:
            import_seed_memories(
                self.database_path, manifest, owner_participant_key="helios"
            )
        self.assertEqual(getattr(caught.exception, "code", None), "seed_import_drift")

    def test_provider_history_visibility_matrix_and_external_welcome_envelope(self) -> None:
        publish_gemini_welcome(self.database_path)
        with closing(connect_database(self.database_path)) as connection:
            participants = {
                row["participant_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id,participant_key FROM participants"
                )
            }
            room_id = connection.execute(
                "SELECT id FROM rooms WHERE room_key='main'"
            ).fetchone()[0]

            room_turn = create_turn(connection, room_id, participants["peter"])
            store_message(
                connection,
                room_id=room_id,
                participant_id=participants["peter"],
                message_text="shared room note",
                turn_id=room_turn,
                message_type="chat",
                sender_alias_id=current_alias(connection, participants["peter"])["id"],
                destination_kind="room",
                destination_alias_id=current_alias(connection, participants["room-system"])["id"],
            )
            connection.execute(
                "UPDATE turns SET status='completed',completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (room_turn,),
            )

            connection.commit()

        from tests.test_room_service import FakeClient as OpenAIFakeClient
        from tests.test_room_service import FakeResponse as OpenAIFakeResponse
        from tests.test_room_service import FakeResponses as OpenAIFakeResponses

        openai_response = OpenAIFakeResponse(
            status="completed",
            output_text="private Helios reply",
            raw={
                "id": "history-openai", "object": "response", "status": "completed",
                "model": "gpt-5.6-luna-resolved", "usage": {},
                "output": [{
                    "id": "history-message", "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "private Helios reply"}],
                }],
            },
        )
        openai_client = OpenAIFakeClient(OpenAIFakeResponses(openai_response))
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-openai", "HELIOS_OPENAI_MODEL": "gpt-5.6-luna"},
            clear=True,
        ):
            asyncio.run(run_helios_turn(
                "private for Helios",
                database_path=self.database_path,
                client_factory=lambda _key: openai_client,
                dotenv_path=self.dotenv_path,
            ))
        self.run_turn(success_response("private Gemini reply"), "private for Gemini")
        with closing(connect_database(self.database_path)) as connection:
            boundary = connection.execute(
                "SELECT max(room_sequence_no) FROM messages"
            ).fetchone()[0]
            helios = load_provider_history(
                connection,
                room_id=room_id,
                boundary=boundary,
                provider_participant_key="helios",
            )
            gemini = load_provider_history(
                connection,
                room_id=room_id,
                boundary=boundary,
                provider_participant_key="gemini",
            )

        self.assertEqual(len(helios), 6)
        self.assertEqual(helios[1], {"role": "user", "content": "shared room note"})
        self.assertEqual(helios[2], {"role": "user", "content": "private for Helios"})
        self.assertEqual(helios[3], {"role": "assistant", "content": "private Helios reply"})
        gemini_text = [part["text"] for content in gemini for part in content["parts"]]
        self.assertEqual(gemini_text[1], "shared room note")
        self.assertIn("private for Helios", gemini_text[2])
        self.assertIn("private Helios reply", gemini_text[3])
        self.assertEqual(gemini_text[4:], [
            "private for Gemini", "private thought", "private Gemini reply"
        ])
        self.assertTrue(gemini_text[0].startswith("ROOM_PARTICIPANT_MESSAGE\n"))
        welcome_envelope = json.loads(gemini_text[0].split("\n", 1)[1])
        self.assertEqual(welcome_envelope["sender"], {
            "display_name": "Helios", "participant_key": "helios"
        })
        self.assertEqual(welcome_envelope["destination"], {
            "display_name": "Gemini", "kind": "participant", "participant_key": "gemini"
        })
        self.assertIn("private for Helios", json.dumps(gemini))
        self.assertIn("private Gemini", json.dumps(helios))

    def test_fixed_exception_diagnostics_never_use_exception_class(self) -> None:
        class AttackerNamedError(RuntimeError):
            code = SYNTHETIC_KEY
            request_id = SYNTHETIC_KEY

        diagnostics = safe_gemini_exception_diagnostics(
            AttackerNamedError("private"), timeout=False, api_key=SYNTHETIC_KEY
        )
        self.assertEqual(diagnostics, {
            "error_class": "ProviderError",
            "reason": "gemini_provider_failure",
            "summary": "The Gemini provider request failed.",
        })

        AttackerTimeoutName = type("ReadTimeout", (RuntimeError,), {})
        self.assertFalse(is_timeout_exception(AttackerTimeoutName("private")))
        self.assertTrue(is_timeout_exception(httpx.ReadTimeout("synthetic timeout")))

        class HostileProperties(RuntimeError):
            @property
            def status_code(self) -> int:
                raise RuntimeError("private getter")

            @property
            def code(self) -> str:
                raise RuntimeError("private getter")

            @property
            def request_id(self) -> str:
                raise RuntimeError("private getter")

        self.assertEqual(
            safe_gemini_exception_diagnostics(
                HostileProperties(), timeout=False, api_key=SYNTHETIC_KEY
            ),
            {
                "error_class": "ProviderError",
                "reason": "gemini_provider_failure",
                "summary": "The Gemini provider request failed.",
            },
        )

    def test_false_thought_flags_are_omitted_and_signature_boundaries_round_trip(self) -> None:
        response = RawResponse(
            {
                "candidates": [{
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "text": "",
                                "thought": True,
                                "thought_signature": b"\xfb\xff\x00",
                            },
                            {"text": "visible", "thought": False},
                            {"text": " tail"},
                        ],
                    },
                    "finish_reason": "STOP",
                    "index": 0,
                }]
            },
            "visible tail",
        )
        evaluated = serialize_and_evaluate_response(response, api_key=SYNTHETIC_KEY)
        parts = evaluated.raw_response["candidates"][0]["content"]["parts"]
        self.assertNotIn("thought", parts[1])
        rebuilt = content_from_recorded(
            evaluated.raw_response["candidates"][0]["content"]
        )
        self.assertEqual(rebuilt.parts[0].thought_signature, b"\xfb\xff\x00")
        self.assertIsNone(rebuilt.parts[1].thought)
        self.assertEqual([part.text for part in rebuilt.parts], ["", "visible", " tail"])

    def test_cancellation_and_finalization_failure_never_retry_provider(self) -> None:
        with self.assertRaises(TurnServiceError) as cancelled:
            self.run_turn(asyncio.CancelledError())
        self.assertEqual(cancelled.exception.code, "turn_finalization_failed")
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "open")
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 1)

        finalization_db = Path(self.temp.name) / "finalization.db"
        initialize_database(finalization_db)
        self.database_path = finalization_db
        client = FakeClient(success_response())
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ), patch(
            "app.gemini_service._finalize_phase_c",
            side_effect=sqlite3.OperationalError("synthetic finalization failure"),
        ):
            with self.assertRaises(TurnServiceError) as stranded:
                asyncio.run(
                    run_gemini_turn(
                        "accepted once",
                        database_path=finalization_db,
                        client_factory=lambda _key: client,
                        dotenv_path=self.dotenv_path,
                    )
                )
        self.assertEqual(stranded.exception.code, "turn_finalization_failed")
        self.assertEqual(len(client.aio.models.calls), 1)
        with closing(connect_database(finalization_db)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "open")
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_phase_c_serialization_and_output_derivation_hold_write_transaction(self) -> None:
        import app.gemini_service as service

        original_assert = service._assert_accepted_evidence
        original_serialize = service.serialize_and_evaluate_response
        state = {"validated_in_transaction": False, "serialized": False}

        def checked_assert(connection: sqlite3.Connection, accepted: Any) -> None:
            self.assertTrue(connection.in_transaction)
            original_assert(connection, accepted)
            state["validated_in_transaction"] = True

        def checked_serialize(response: Any, *, api_key: str) -> Any:
            self.assertTrue(state["validated_in_transaction"])
            state["serialized"] = True
            return original_serialize(response, api_key=api_key)

        with patch.object(service, "_assert_accepted_evidence", side_effect=checked_assert), patch.object(
            service, "serialize_and_evaluate_response", side_effect=checked_serialize
        ):
            self.run_turn(success_response("inside Phase C"))
        self.assertTrue(state["serialized"])

    def test_phase_c_rejects_mutated_accepted_evidence_without_output(self) -> None:
        database_path = self.database_path

        class MutatingModels(FakeModels):
            async def generate_content(self, **request: Any) -> Any:
                self.calls.append(request)
                with closing(connect_database(database_path)) as connection:
                    connection.execute(
                        "UPDATE api_events SET payload_json='{}' WHERE sequence_no=1"
                    )
                    connection.commit()
                return success_response("must not be stored")

        client = FakeClient(success_response())
        client.aio.models = MutatingModels(success_response())
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "gemini-test"},
            clear=True,
        ):
            with self.assertRaises(TurnServiceError) as caught:
                asyncio.run(
                    run_gemini_turn(
                        "accepted evidence",
                        database_path=database_path,
                        client_factory=lambda _key: client,
                        dotenv_path=self.dotenv_path,
                    )
                )
        self.assertEqual(caught.exception.code, "turn_finalization_failed")
        self.assertEqual(len(client.aio.models.calls), 1)
        with closing(connect_database(database_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone()[0], "open")
            self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages WHERE participant_config_id IS NOT NULL").fetchone()[0],
                0,
            )

    def test_role_specific_parts_and_response_semantics_fail_deterministically(self) -> None:
        for user_part in (
            {"text": "x", "thought": True},
            {"text": "x", "thought_signature_b64": "AA=="},
            {"text": "x", "extra": 1},
        ):
            with self.subTest(user_part=user_part), self.assertRaises(ValueError):
                content_from_recorded({"role": "user", "parts": [user_part]})
        with self.assertRaises(ValueError):
            content_from_recorded(
                {"role": "model", "parts": [{"text": "x", "thought": False}]}
            )

        for block_reason in (1, True, [], {}):
            raw = success_response("visible").model_dump(mode="python", exclude_none=True)
            raw["prompt_feedback"] = {"block_reason": block_reason}
            evaluation = serialize_and_evaluate_response(
                RawResponse(raw, "visible"), api_key=SYNTHETIC_KEY
            )
            with self.subTest(block_reason=block_reason):
                self.assertEqual(evaluation.failure_kind, "invalid_prompt_feedback")

        for hostile_rating in (
            {"category": "HARM", "probability": "LOW", "future_detail": "private"},
            {"category": "HARM", "probability": "LOW", "blocked": "false"},
            {"category": "", "probability": "LOW"},
        ):
            raw = success_response("visible").model_dump(mode="python", exclude_none=True)
            raw["candidates"][0]["safety_ratings"] = [hostile_rating]
            evaluation = serialize_and_evaluate_response(
                RawResponse(raw, "visible"), api_key=SYNTHETIC_KEY
            )
            with self.subTest(hostile_rating=hostile_rating):
                self.assertEqual(evaluation.failure_kind, "invalid_safety_metadata")

    def test_response_semantic_boundary_and_malformed_signature_matrix(self) -> None:
        cases: list[tuple[str, Any, str]] = []

        def candidate_case(name: str, change: Any, expected: str) -> None:
            raw = success_response("visible").model_dump(mode="python", exclude_none=True)
            change(raw)
            cases.append((name, raw, expected))

        candidate_case(
            "finish",
            lambda raw: raw["candidates"][0].__setitem__("finish_reason", "MAX_TOKENS"),
            "invalid_finish_reason",
        )
        candidate_case(
            "index_boolean",
            lambda raw: raw["candidates"][0].__setitem__("index", True),
            "invalid_candidate_index",
        )
        candidate_case(
            "role",
            lambda raw: raw["candidates"][0]["content"].__setitem__("role", "user"),
            "malformed_candidate_content",
        )
        candidate_case(
            "empty_parts",
            lambda raw: raw["candidates"][0]["content"].__setitem__("parts", []),
            "malformed_candidate_content",
        )
        candidate_case(
            "unsupported_part",
            lambda raw: raw["candidates"][0]["content"]["parts"][1].__setitem__("function_call", {}),
            "unsupported_output_part",
        )
        candidate_case(
            "usage_boolean",
            lambda raw: raw["usage_metadata"].__setitem__("prompt_token_count", True),
            "invalid_usage_metadata",
        )
        candidate_case(
            "usage_above_max",
            lambda raw: raw["usage_metadata"].__setitem__("prompt_token_count", MAX_SAFE_INTEGER + 1),
            "serialization_failure",
        )
        candidate_case(
            "usage_total_too_small",
            lambda raw: raw["usage_metadata"].update(
                {"prompt_token_count": 2, "total_token_count": 1}
            ),
            "invalid_usage_metadata",
        )

        for name, raw, expected in cases:
            with self.subTest(name=name):
                if expected == "serialization_failure":
                    with self.assertRaises(Exception):
                        serialize_and_evaluate_response(
                            RawResponse(raw, "visible"), api_key=SYNTHETIC_KEY
                        )
                else:
                    evaluated = serialize_and_evaluate_response(
                        RawResponse(raw, "visible"), api_key=SYNTHETIC_KEY
                    )
                    self.assertEqual(evaluated.failure_kind, expected)

        stored = {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [{"text": "visible", "thought_signature_b64": "-_8"}],
                },
                "finish_reason": "STOP",
                "index": 0,
            }]
        }
        with self.assertRaises(ValueError):
            validate_stored_success_response(stored)

    def test_corrupt_historical_google_requests_fail_before_provider_invocation(self) -> None:
        def fail_fast(label: str) -> Any:
            def reject(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError(label)

            return reject

        fail_fast_probe = fail_fast("historical boundary self-test")
        with self.assertRaisesRegex(AssertionError, "historical boundary self-test"):
            fail_fast_probe()

        def mutate_timeout(payload: dict[str, Any]) -> None:
            payload["local_context"]["timeout_seconds"] = 121

        def mutate_model(payload: dict[str, Any]) -> None:
            payload["request"]["model"] = "different-model"

        def mutate_configuration(payload: dict[str, Any]) -> None:
            payload["request"]["config"]["candidate_count"] = 2

        def mutate_user_part(payload: dict[str, Any]) -> None:
            payload["request"]["contents"][-1]["parts"][0]["thought"] = True

        def mutate_trigger(payload: dict[str, Any]) -> None:
            payload["local_context"]["trigger_message_id"] += 1

        def mutate_memory_owner(payload: dict[str, Any]) -> None:
            payload["local_context"]["memory_retrieval"]["owner_participant_id"] += 1

        def mutate_envelope(payload: dict[str, Any]) -> None:
            payload["unexpected"] = None

        def mutate_trigger_placement(payload: dict[str, Any]) -> None:
            payload["request"]["contents"].append(
                {"role": "user", "parts": [{"text": "misplaced trigger"}]}
            )

        request_cases = (
            mutate_envelope,
            mutate_timeout,
            mutate_model,
            mutate_configuration,
            mutate_user_part,
            mutate_trigger,
            mutate_trigger_placement,
            mutate_memory_owner,
        )
        cases = tuple((mutator.__name__, mutator) for mutator in request_cases) + (
            ("response_replay_role", None),
        )
        for index, (name, mutator) in enumerate(cases):
            with self.subTest(corruption=name):
                database_path = Path(self.temp.name) / f"history-{index}.db"
                initialize_database(database_path)
                self.database_path = database_path
                self.run_turn(success_response("prior Gemini reply"), "first trigger")
                with closing(connect_database(database_path)) as connection:
                    request_row = connection.execute(
                        "SELECT id,payload_json FROM api_events WHERE event_type='google.generate_content.request'"
                    ).fetchone()
                    if mutator is not None:
                        payload = json.loads(request_row["payload_json"])
                        mutator(payload)
                        connection.execute(
                            "UPDATE api_events SET payload_json=? WHERE id=?",
                            (
                                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                                request_row["id"],
                            ),
                        )
                    else:
                        response_row = connection.execute(
                            """SELECT id,payload_json FROM api_events
                               WHERE event_type='google.generate_content.response'"""
                        ).fetchone()
                        response_payload = json.loads(response_row["payload_json"])
                        response_payload["response"]["candidates"][0]["content"]["role"] = "user"
                        connection.execute(
                            "UPDATE api_events SET payload_json=? WHERE id=?",
                            (
                                json.dumps(
                                    response_payload,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                                response_row["id"],
                            ),
                        )
                    connection.commit()
                before_snapshot = logical_database_snapshot(database_path)
                self.assertEqual(
                    set(before_snapshot), {"pragmas", "schema", "sequence", "tables"}
                )
                self.assertTrue(before_snapshot["schema"])
                self.assertTrue(before_snapshot["tables"])
                synthetic_environment = {
                    "GEMINI_API_KEY": SYNTHETIC_KEY,
                    "HELIOS_GEMINI_MODEL": "gemini-test",
                }
                with patch.object(os, "environ", synthetic_environment), patch(
                    "app.gemini_service.create_gemini_client",
                    side_effect=fail_fast("client factory must not run"),
                ) as client_constructor, patch(
                    "app.gemini_service.create_gemini_response",
                    side_effect=fail_fast("provider invocation must not run"),
                ) as provider_call:
                    with self.assertRaises(TurnServiceError) as caught:
                        asyncio.run(
                            run_gemini_turn(
                                "second trigger",
                                database_path=database_path,
                                dotenv_path=self.dotenv_path,
                            )
                        )
                self.assertEqual(caught.exception.code, "unsupported_gemini_provider_history")
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(
                    caught.exception.as_payload(),
                    {
                        "error": "unsupported_gemini_provider_history",
                        "message": "Gemini provider history cannot be reconstructed safely.",
                    },
                )
                self.assertEqual(client_constructor.call_count, 0)
                self.assertEqual(provider_call.call_count, 0)
                after_snapshot = logical_database_snapshot(database_path)
                self.assertEqual(after_snapshot, before_snapshot)

    def test_exact_google_request_field_corruption_matrix_uses_shared_validator(self) -> None:
        self.run_turn(success_response("recorded"), "trigger")
        with closing(connect_database(self.database_path)) as connection:
            valid = json.loads(
                connection.execute(
                    "SELECT payload_json FROM api_events WHERE event_type='google.generate_content.request'"
                ).fetchone()[0]
            )

        def delete(path: tuple[str, ...]) -> Any:
            def mutate(payload: dict[str, Any]) -> None:
                target: dict[str, Any] = payload
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
            return mutate

        def replace(path: tuple[str, ...], value: Any) -> Any:
            def mutate(payload: dict[str, Any]) -> None:
                target: dict[str, Any] = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return mutate

        corruptions = {
            "envelope_extra": lambda payload: payload.__setitem__("extra", None),
            "missing_local": delete(("local_context",)),
            "api_version": replace(("local_context", "api_version"), "v1"),
            "operation": replace(("local_context", "operation"), "other"),
            "provider": replace(("local_context", "provider"), "openai"),
            "boundary": replace(("local_context", "room_sequence_boundary"), 0),
            "safety": replace(("local_context", "safety_settings"), []),
            "sdk_policy": replace(("local_context", "sdk_policy"), {"automatic_function_calling": {"disable": False}}),
            "timeout": replace(("local_context", "timeout_seconds"), 1),
            "attempts": replace(("local_context", "total_attempts"), 2),
            "trigger": replace(("local_context", "trigger_message_id"), 0),
            "memory_type": replace(("local_context", "memory_retrieval"), []),
            "request_extra": lambda payload: payload["request"].__setitem__("extra", None),
            "model_blank": replace(("request", "model"), " \t"),
            "config_missing": delete(("request", "config", "tools")),
            "candidate_count": replace(("request", "config", "candidate_count"), 2),
            "max_output": replace(("request", "config", "max_output_tokens"), 2049),
            "modalities": replace(("request", "config", "response_modalities"), ["IMAGE"]),
            "system": replace(("request", "config", "system_instruction"), "changed"),
            "thinking": replace(("request", "config", "thinking_config"), {"include_thoughts": True, "thinking_level": "medium"}),
            "tools": replace(("request", "config", "tools"), [{}]),
            "contents_empty": replace(("request", "contents"), []),
            "user_extra": lambda payload: payload["request"]["contents"][-1]["parts"][0].__setitem__("extra", 1),
            "user_signature": lambda payload: payload["request"]["contents"][-1]["parts"][0].__setitem__("thought_signature_b64", "AA=="),
        }
        for name, mutate in corruptions.items():
            payload = json.loads(json.dumps(valid))
            mutate(payload)
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_recorded_google_shared_request_payload(payload)

    def test_historical_event_metadata_and_mixed_families_fail_before_provider(self) -> None:
        def fail_fast(label: str) -> Any:
            def reject(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError(label)

            return reject

        fail_fast_probe = fail_fast("metadata boundary self-test")
        with self.assertRaisesRegex(AssertionError, "metadata boundary self-test"):
            fail_fast_probe()

        for index, corruption in enumerate(
            (
                "participant_configuration_owner",
                "request_sequence",
                "response_correlation",
                "mixed_family",
            )
        ):
            with self.subTest(corruption=corruption):
                database_path = Path(self.temp.name) / f"event-{index}.db"
                initialize_database(database_path)
                self.database_path = database_path
                self.run_turn(success_response("prior"), "first")
                with closing(connect_database(database_path)) as connection:
                    request = connection.execute(
                        """SELECT id,turn_id,room_id,related_message_id
                           FROM api_events WHERE event_type='google.generate_content.request'"""
                    ).fetchone()
                    helios = connection.execute(
                        """SELECT p.id,pc.id FROM participants AS p
                           JOIN participant_configs AS pc ON pc.participant_id=p.id
                           WHERE p.participant_key='helios' AND pc.config_label='initial'"""
                    ).fetchone()
                    if corruption == "participant_configuration_owner":
                        connection.execute(
                            """UPDATE api_events
                               SET participant_id=?, participant_config_id=? WHERE id=?""",
                            (helios[0], helios[1], request["id"]),
                        )
                    elif corruption == "request_sequence":
                        connection.execute(
                            "UPDATE api_events SET sequence_no=3 WHERE id=?",
                            (request["id"],),
                        )
                    elif corruption == "response_correlation":
                        connection.execute(
                            """UPDATE api_events SET related_message_id=?
                               WHERE event_type='google.generate_content.response'""",
                            (request["related_message_id"],),
                        )
                    else:
                        connection.execute(
                            """INSERT INTO api_events
                               (turn_id,room_id,participant_id,participant_config_id,
                                sequence_no,event_type,related_message_id,payload_json)
                               VALUES (?,?,?,?,3,'openai.responses.response',?,'{}')""",
                            (
                                request["turn_id"],
                                request["room_id"],
                                helios[0],
                                helios[1],
                                request["related_message_id"],
                            ),
                        )
                    connection.commit()
                before_snapshot = logical_database_snapshot(database_path)
                self.assertEqual(
                    set(before_snapshot), {"pragmas", "schema", "sequence", "tables"}
                )
                self.assertTrue(before_snapshot["schema"])
                self.assertTrue(before_snapshot["tables"])
                synthetic_environment = {
                    "GEMINI_API_KEY": SYNTHETIC_KEY,
                    "HELIOS_GEMINI_MODEL": "gemini-test",
                }
                with patch.object(os, "environ", synthetic_environment), patch(
                    "app.gemini_service.create_gemini_client",
                    side_effect=fail_fast("client factory must not run"),
                ) as client_constructor, patch(
                    "app.gemini_service.create_gemini_response",
                    side_effect=fail_fast("provider invocation must not run"),
                ) as provider_call:
                    with self.assertRaises(TurnServiceError) as caught:
                        asyncio.run(
                            run_gemini_turn(
                                "second",
                                database_path=database_path,
                                dotenv_path=self.dotenv_path,
                            )
                        )
                self.assertEqual(caught.exception.code, "unsupported_gemini_provider_history")
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(
                    caught.exception.as_payload(),
                    {
                        "error": "unsupported_gemini_provider_history",
                        "message": "Gemini provider history cannot be reconstructed safely.",
                    },
                )
                self.assertEqual(client_constructor.call_count, 0)
                self.assertEqual(provider_call.call_count, 0)
                after_snapshot = logical_database_snapshot(database_path)
                self.assertEqual(after_snapshot, before_snapshot)

    def test_corrupt_gemini_trace_is_isolated_from_unrelated_room_turn(self) -> None:
        result, _client = self.run_turn(success_response("prior"), "first")
        with closing(connect_database(self.database_path)) as connection:
            row = connection.execute(
                "SELECT id,payload_json FROM api_events WHERE event_type='google.generate_content.request'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["request"]["contents"][-1]["parts"][0]["thought"] = True
            connection.execute(
                "UPDATE api_events SET payload_json=? WHERE id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["id"]),
            )
            connection.commit()
        with self.assertRaises(TraceServiceError) as invalid:
            load_trace(self.database_path, result["turn_id"])
        self.assertEqual(invalid.exception.code, "trace_data_invalid")
        room_result = post_room_message("unrelated Room post", self.database_path)
        unrelated = load_trace(self.database_path, room_result["turn_id"])
        self.assertEqual(unrelated["turn"]["id"], room_result["turn_id"])
        self.assertEqual(unrelated["api_events"], [])

    def test_missing_environment_dotenv_precedence_and_configuration_matrix(self) -> None:
        for environment, expected in (
            ({"HELIOS_GEMINI_MODEL": "gemini-test"}, "missing_gemini_api_key"),
            ({"GEMINI_API_KEY": SYNTHETIC_KEY}, "missing_gemini_model"),
            ({"GEMINI_API_KEY": " \t ", "HELIOS_GEMINI_MODEL": "gemini-test"}, "missing_gemini_api_key"),
            ({"GEMINI_API_KEY": SYNTHETIC_KEY, "HELIOS_GEMINI_MODEL": "\n"}, "missing_gemini_model"),
            ({"GOOGLE_API_KEY": "hostile", "HELIOS_GEMINI_MODEL": "gemini-test"}, "missing_gemini_api_key"),
        ):
            with self.subTest(expected=expected), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(TurnServiceError) as caught:
                    asyncio.run(
                        run_gemini_turn(
                            "zero writes",
                            database_path=self.database_path,
                            client_factory=lambda _key: (_ for _ in ()).throw(AssertionError("provider")),
                            dotenv_path=self.dotenv_path,
                        )
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertIsNone(caught.exception.turn_id)
                with closing(connect_database(self.database_path)) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM api_events").fetchone()[0], 0)
                    self.assertEqual(
                        connection.execute(
                            """SELECT count(*) FROM participant_configs AS pc
                               JOIN participants AS p ON p.id=pc.participant_id
                               WHERE p.participant_key='gemini'"""
                        ).fetchone()[0],
                        0,
                    )

        dotenv = Path(self.temp.name) / "provider.env"
        dotenv.write_text(
            "GEMINI_API_KEY=dotenv-key\nHELIOS_GEMINI_MODEL=dotenv-model\nGOOGLE_API_KEY=ignored\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "process-key", "HELIOS_GEMINI_MODEL": "process-model"},
            clear=True,
        ):
            loaded = load_gemini_environment(dotenv)
        self.assertEqual((loaded.api_key, loaded.model), ("process-key", "process-model"))

        with closing(connect_database(self.database_path)) as connection:
            gemini_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='gemini'"
            ).fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            first = find_or_create_gemini_configuration(
                connection, gemini_id=gemini_id, model="Model / Alpha"
            )
            reused = find_or_create_gemini_configuration(
                connection, gemini_id=gemini_id, model="Model / Alpha"
            )
            second_model = find_or_create_gemini_configuration(
                connection, gemini_id=gemini_id, model="Model Beta"
            )
            connection.commit()
            self.assertEqual(first, reused)
            self.assertNotEqual(first, second_model)
            labels = [
                row[0]
                for row in connection.execute(
                    "SELECT config_label FROM participant_configs WHERE participant_id=? ORDER BY id",
                    (gemini_id,),
                )
            ]
            self.assertEqual(
                labels,
                ["direct-address-google-model-alpha-v1", "direct-address-google-model-beta-v1"],
            )
            connection.execute(
                """INSERT INTO participant_configs
                   (participant_id,provider,model,config_label,system_instructions,settings_json,tools_json)
                   VALUES (?, 'google', 'Model / Alpha',
                           'direct-address-google-model-alpha-vbad', ?, '{}', '[]')""",
                (gemini_id, GEMINI_SYSTEM_INSTRUCTIONS_V2),
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(TurnServiceError) as malformed:
                find_or_create_gemini_configuration(
                    connection, gemini_id=gemini_id, model="Model / Alpha"
                )
            self.assertEqual(malformed.exception.code, "invalid_database_configuration")
            connection.rollback()

        versioned = Path(self.temp.name) / "configuration-version.db"
        initialize_database(versioned)
        with closing(connect_database(versioned)) as connection:
            gemini_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='gemini'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO participant_configs
                   (participant_id,provider,model,config_label,system_instructions,settings_json,tools_json)
                   VALUES (?, 'local', 'Model / Alpha',
                           'seed-memory-google-model-alpha-v1', NULL, '{}', '[]')""",
                (gemini_id,),
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            created = find_or_create_gemini_configuration(
                connection, gemini_id=gemini_id, model="Model / Alpha"
            )
            label = connection.execute(
                "SELECT config_label FROM participant_configs WHERE id=?", (created,)
            ).fetchone()[0]
            self.assertEqual(label, "direct-address-google-model-alpha-v1")
            connection.rollback()

    def test_installer_and_welcome_partial_conflict_and_ordering_matrix(self) -> None:
        # A partial Gemini identity is incompatible and the failed installer is zero-write.
        partial = Path(self.temp.name) / "partial-installer.db"
        connection = connect_database(partial)
        try:
            connection.executescript(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute("BEGIN IMMEDIATE")
            room_id = _ensure_room(connection)
            peter_id = _ensure_participant(connection, "peter", "Peter", "human")
            helios_id = _ensure_participant(connection, "helios", "Helios", "ai")
            system_id = _ensure_participant(connection, "room-system", "Room", "system")
            for participant_id, alias in ((peter_id, "Peter"), (helios_id, "Helios"), (system_id, "Room")):
                _ensure_identity_bootstrap(connection, participant_id, alias)
            _ensure_active_membership(connection, room_id, peter_id)
            _ensure_active_membership(connection, room_id, helios_id)
            _ensure_initial_helios_config(connection, helios_id)
            connection.execute(
                "INSERT INTO participants (participant_key,name,participant_type) VALUES ('gemini','Gemini','ai')"
            )
            connection.commit()
            before = connection.execute("SELECT count(*) FROM participants").fetchone()[0]
        finally:
            connection.close()
        with self.assertRaises(GeminiIdentityError) as incompatible:
            install_gemini(partial)
        self.assertEqual(incompatible.exception.code, "gemini_install_incompatible")
        with closing(connect_database(partial)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM participants").fetchone()[0], before)
            gemini_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='gemini'"
            ).fetchone()[0]
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM participant_aliases WHERE participant_id=?", (gemini_id,)
                ).fetchone()[0],
                0,
            )

        recoverable = Path(self.temp.name) / "welcome-recoverable.db"
        initialize_database(recoverable)
        with closing(connect_database(recoverable)) as connection:
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='helios'"
            ).fetchone()[0]
            settings = json.dumps(
                _welcome_settings(), sort_keys=True, separators=(",", ":")
            )
            config_id = connection.execute(
                """INSERT INTO participant_configs
                   (participant_id,provider,model,config_label,system_instructions,settings_json,tools_json)
                   VALUES (?, 'local', NULL, ?, NULL, ?, '[]')""",
                (helios_id, WELCOME_CONFIG_LABEL, settings),
            ).lastrowid
            connection.commit()
        recovered = publish_gemini_welcome(recoverable)
        self.assertEqual(recovered["status"], "published")
        with closing(connect_database(recoverable)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT participant_config_id FROM messages WHERE id=?",
                    (recovered["message_id"],),
                ).fetchone()[0],
                config_id,
            )

        # Classification of an absent welcome precedes the first write.
        import app.gemini_identity as identity

        original_connect = identity.connect_database
        observed = {"classified": False, "write_after_classification": False}

        def instrumented_connect(path: Any) -> sqlite3.Connection:
            connection = original_connect(path)

            def trace(statement: str) -> None:
                normalized = " ".join(statement.split()).upper()
                if "FROM MESSAGES AS M" in normalized:
                    observed["classified"] = True
                if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ")):
                    self.assertTrue(observed["classified"])
                    observed["write_after_classification"] = True

            connection.set_trace_callback(trace)
            return connection

        with patch.object(identity, "connect_database", side_effect=instrumented_connect):
            published = publish_gemini_welcome(self.database_path)
        self.assertEqual(published["status"], "published")
        self.assertTrue(observed["write_after_classification"])
        with closing(connect_database(self.database_path)) as connection:
            message = connection.execute(
                "SELECT id,turn_id,room_id,participant_id,participant_config_id FROM messages"
            ).fetchone()
            connection.execute(
                """INSERT INTO api_events
                   (turn_id,room_id,participant_id,participant_config_id,
                    sequence_no,event_type,related_message_id,payload_json)
                   VALUES (?,?,?,?,1,'synthetic.extra',?,'{}')""",
                (
                    message["turn_id"],
                    message["room_id"],
                    message["participant_id"],
                    message["participant_config_id"],
                    message["id"],
                ),
            )
            connection.commit()
            before_counts = tuple(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("participant_configs", "turns", "messages", "api_events")
            )
        with self.assertRaises(GeminiIdentityError) as extra_event:
            publish_gemini_welcome(self.database_path)
        self.assertEqual(extra_event.exception.code, "gemini_welcome_state_incompatible")
        with closing(connect_database(self.database_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("participant_configs", "turns", "messages", "api_events")
                ),
                before_counts,
            )

        conflicting = Path(self.temp.name) / "welcome-conflict.db"
        initialize_database(conflicting)
        with closing(connect_database(conflicting)) as connection:
            helios_id = connection.execute(
                "SELECT id FROM participants WHERE participant_key='helios'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO participant_configs
                   (participant_id,provider,model,config_label,system_instructions,settings_json,tools_json)
                   VALUES (?, 'local', NULL, ?, NULL, '{}', '[]')""",
                (helios_id, WELCOME_CONFIG_LABEL),
            )
            connection.commit()
            before = connection.execute("SELECT count(*) FROM participant_configs").fetchone()[0]
        with self.assertRaises(GeminiIdentityError) as conflict:
            publish_gemini_welcome(conflicting)
        self.assertEqual(conflict.exception.code, "gemini_welcome_state_incompatible")
        with closing(connect_database(conflicting)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM participant_configs").fetchone()[0], before)
            self.assertEqual(connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_cross_owner_batch_conflicts_and_distinct_hash_policy(self) -> None:
        source = Path(__file__).resolve().parent.parent / "examples" / "seed-memory-manifest-v1.example.json"
        original = load_seed_manifest(source)
        first = import_seed_memories(
            self.database_path, original, owner_participant_key="helios"
        )
        changed_value = json.loads(original.canonical_json)
        changed_value["batch"]["notes"] = "distinct synthetic source"
        changed = parse_seed_manifest(
            json.dumps(changed_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        second = import_seed_memories(
            self.database_path, changed, owner_participant_key="gemini"
        )
        self.assertNotEqual(first["source_content_sha256"], second["source_content_sha256"])
        third_value = json.loads(original.canonical_json)
        third_value["batch"]["notes"] = "third synthetic source"
        third = parse_seed_manifest(
            json.dumps(third_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        with self.assertRaises(Exception) as same_owner:
            import_seed_memories(
                self.database_path, third, owner_participant_key="helios"
            )
        self.assertEqual(getattr(same_owner.exception, "code", None), "seed_stable_id_conflict")

    def test_all_omission_pointer_collections_are_sorted_unique_and_escaped(self) -> None:
        value = [
            {"safe": index}
            for index in range(12)
        ]
        value[2] = {
            "google/api_key~": "secret",
            "thought_signature_b64": "AA==",
            "nested": {"client_secret": "secret"},
        }
        value[10] = {
            "thought": True,
            "text": "private",
            "x-goog-api-key": "secret",
        }
        _projected, pointers = project_trace_json(value, ("request", "contents"))
        self.assertEqual(pointers, sorted(set(pointers)))
        self.assertIn("/request/contents/2/google~1api_key~0", pointers)
        self.assertIn("/request/contents/2/thought_signature_b64", pointers)
        self.assertIn("/request/contents/2/nested/client_secret", pointers)
        self.assertIn("/request/contents/10/text", pointers)
        self.assertIn("/request/contents/10/x-goog-api-key", pointers)


if __name__ == "__main__":
    unittest.main()
