"""Scripted CLI contracts: never connect to or capture a real terminal."""

import json
import os
from pathlib import Path

import pytest

from answerback_bridge.process import BridgeError, CommandCleanupError, Output

S = "session:11111111-1111-4111-8111-111111111111"
B = "block:22222222-2222-4222-8222-222222222222"
ENDPOINT = "unix:///tmp/rex-test.sock"


@pytest.fixture
def native_schemas(cli, monkeypatch):
    """Replay sanitized API 11 descriptions captured from the installed provider."""
    from answerback_bridge import rex

    schemas = json.loads((Path(__file__).parent / "fixtures" / "rex_api_schemas.json").read_text())
    original = cli.run

    async def run(argv, **kwargs):
        if "describe" in argv:
            return Output(json.dumps(schemas[argv[-2]]), "")
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", run)
    return schemas


async def test_native_api_schema_shapes_support_read_only_probe(native_schemas):
    from answerback_bridge.rex import Rex

    assert "properties" not in native_schemas["session.list"]["input_schema"]
    assert native_schemas["session.list"]["output_schema"]["properties"]["sessions"]["type"] == [
        "null",
        "array",
    ]
    assert all((await Rex("/fixture/rex", server=ENDPOINT).probe())["capabilities"].values())


@pytest.mark.parametrize(
    "method,capability",
    [
        ("session.list", "discovery"),
        ("session.list_blocks", "discovery"),
        ("session.describe_block", "discovery"),
        ("com.superlogical.terminal.process", "process"),
        ("com.superlogical.terminal.program_status", "program_status"),
        ("com.superlogical.terminal.format", "read"),
    ],
)
async def test_unsupported_required_native_input_disables_capability(
    native_schemas, method, capability
):
    from answerback_bridge.rex import Rex

    description = native_schemas[method]
    schema = description.get("args_schema", description.get("input_schema"))
    schema.setdefault("properties", {})["unsupported"] = {"type": "string"}
    schema.setdefault("required", []).append("unsupported")
    capabilities = (await Rex("/fixture/rex", server=ENDPOINT).probe())["capabilities"]
    assert not capabilities[capability]
    if method == "com.superlogical.terminal.process":
        assert not capabilities["enroll"]
    unaffected = set(capabilities) - {capability}
    if method == "com.superlogical.terminal.process":
        unaffected.remove("enroll")
    assert all(capabilities[key] for key in unaffected)


@pytest.mark.parametrize(
    "method,capability",
    [
        ("session.list", "discovery"),
        ("com.superlogical.terminal.process", "process"),
        ("com.superlogical.terminal.program_status", "program_status"),
    ],
)
async def test_missing_native_input_schema_disables_capability(native_schemas, method, capability):
    from answerback_bridge.rex import Rex

    description = native_schemas[method]
    description.pop("args_schema", None)
    description.pop("input_schema", None)
    capabilities = (await Rex("/fixture/rex", server=ENDPOINT).probe())["capabilities"]
    assert not capabilities[capability]
    unaffected = set(capabilities) - {capability}
    if method == "com.superlogical.terminal.process":
        assert not capabilities["enroll"]
        unaffected.remove("enroll")
    assert all(capabilities[key] for key in unaffected)


@pytest.mark.parametrize(
    "schema",
    [
        None,
        [],
        {"type": "string"},
        {"type": ["object", "string"]},
        {"type": "object", "properties": []},
        {"type": "object", "required": "unsupported"},
        {"type": "object", "required": [42]},
    ],
)
async def test_malformed_native_no_argument_schema_is_rejected(native_schemas, schema):
    from answerback_bridge.rex import Rex

    native_schemas["session.list"]["input_schema"] = schema
    assert not (await Rex("/fixture/rex", server=ENDPOINT).probe())["capabilities"]["discovery"]


@pytest.fixture
def cli(monkeypatch):
    from answerback_bridge import rex

    class Script:
        identity = {
            "endpoint": ENDPOINT,
            "instance_id": "run-1",
            "host_id": "local",
            "api_revision": 11,
            "principal": {"transport": "unix"},
        }
        block = {
            "block_id": B,
            "creator_name": "com.superlogical.terminal",
            "label": "test",
            "placement": "detached",
            "host_id": "local",
        }
        foreground = {"pid": 42, "user_id": os.getuid(), "name": "python", "cwd": "/project"}
        records = []
        capture = {"content": "é", "total": 2, "eof": True}
        missing = set()
        overrides = {}
        calls = []

        async def run(self, argv, *, env, max_seconds, cleanup_seconds):
            self.calls.append(argv)
            assert "--autostart=false" in argv
            assert not any(
                k in env for k in ("REX_SERVER", "REX_SESSION", "REX_BLOCK", "REX_CLIENT")
            )
            assert max_seconds <= 3
            if "whoami" in argv:
                value = self.identity
            elif "describe" in argv:
                method = argv[-2]
                if method in self.missing:
                    raise BridgeError("not_found")
                fields = {
                    "session.list": ([], ["sessions"]),
                    "session.list_blocks": (["session_id"], ["blocks"]),
                    "session.describe_block": (["session_id", "block_id"], ["block"]),
                    "com.superlogical.terminal.process": ([], ["foreground"]),
                    "com.superlogical.terminal.program_status": ([], ["records"]),
                    "com.superlogical.terminal.format": (["max_bytes", "format"], ["content"]),
                }
                inputs, outputs = fields[method]
                value = {
                    "invoke": {"method": method},
                    "input_schema": {
                        "properties": {
                            k: {"type": "integer" if k == "max_bytes" else "string"} for k in inputs
                        }
                    },
                    "args_schema": {
                        "properties": {
                            k: {"type": "integer" if k == "max_bytes" else "string"} for k in inputs
                        }
                    },
                    "output_schema": {
                        "properties": {
                            k: {
                                "type": {
                                    "sessions": "array",
                                    "blocks": "array",
                                    "records": "array",
                                    "block": "object",
                                    "foreground": "object",
                                    "content": "string",
                                }[k]
                            }
                            for k in outputs
                        }
                    },
                }
            else:
                method = argv[-2]
                value = self.overrides.get(
                    method,
                    {
                        "session.list": {"sessions": [{"session_id": S, "label": "test"}]},
                        "session.list_blocks": {"blocks": [self.block]},
                        "session.describe_block": {"block": self.block},
                        "com.superlogical.terminal.process": {"foreground": self.foreground},
                        "com.superlogical.terminal.program_status": {"records": self.records},
                        "com.superlogical.terminal.format": self.capture,
                    }[method],
                )
                if isinstance(value, Exception):
                    raise value
            return Output(json.dumps(value), "")

    script = Script()
    monkeypatch.setattr(rex, "run", script.run)
    for key in ("REX_SERVER", "REX_SESSION", "REX_BLOCK", "REX_CLIENT"):
        monkeypatch.setenv(key, "wrong")
    return script


async def test_explicit_endpoint_and_default_pinning(cli):
    from answerback_bridge.rex import Rex

    adapter = Rex("/fixture/rex")
    assert (await adapter.probe())["capabilities"]["read"]
    await adapter.inventory()
    assert "-S" not in cli.calls[0]
    assert all(c[c.index("-S") + 1] == ENDPOINT for c in cli.calls[1:])
    assert not any(word in c for c in cli.calls for word in ("send", "send-key", "write", "paste"))


@pytest.mark.parametrize(
    "field,value",
    [("host_id", None), ("instance_id", None), ("endpoint", "ssh://remote"), ("host_id", "other")],
)
async def test_identity_fails_closed(cli, field, value):
    from answerback_bridge.rex import Rex

    cli.identity = {**cli.identity, field: value}
    with pytest.raises(BridgeError):
        await Rex("/fixture/rex", server=ENDPOINT, expected_host_id="local").probe()


async def test_restart_invalidates_capabilities(cli):
    from answerback_bridge.rex import Rex

    adapter = Rex("/fixture/rex", server=ENDPOINT)
    await adapter.probe()
    cli.identity = {**cli.identity, "instance_id": "run-2"}
    cli.missing = {"com.superlogical.terminal.format"}
    assert not (await adapter.probe())["capabilities"]["read"]


async def test_terminal_exact_binding_and_uncertain_status(cli):
    from answerback_bridge.rex import Rex

    cli.records = [
        {"state": "unknown", "owner": {"pid": 42, "name": "python"}},
        {"state": "idle", "owner": {"pid": 8, "name": "python"}},
    ]
    result = await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert result["local"] and result["project"] == "/project"
    assert result["binding"]["foreground_pid"] == 42
    assert all(not r["owner_matches"] or not r["known_state"] for r in result["reported_status"])
    assert result["placement"] == "detached"


@pytest.mark.parametrize(
    "change",
    [{"host_id": None}, {"host_id": "remote"}, {"creator_name": "other"}, {"block_id": "suffix"}],
)
async def test_unsafe_blocks_cannot_read(cli, change):
    from answerback_bridge.rex import Rex

    cli.block = {**cli.block, **change}
    with pytest.raises(BridgeError):
        await Rex("/fixture/rex", server=ENDPOINT).read(S, B)
    assert not any("com.superlogical.terminal.format" in c and "call" in c for c in cli.calls)


@pytest.mark.parametrize("uid", [None, str(os.getuid()), True, os.getuid() + 1])
async def test_read_capability_requires_owned_foreground(cli, uid):
    """red_at_base: a local terminal also needs strict current-uid evidence for read."""
    from answerback_bridge.rex import Rex

    cli.foreground = {**cli.foreground, "user_id": uid}
    adapter = Rex("/fixture/rex", server=ENDPOINT)
    assert (await adapter.terminal(S, B))["capabilities"] == {"read": False, "enroll": False}
    with pytest.raises(BridgeError, match="local bounded-read capability"):
        await adapter.read(S, B)
    assert not any(
        "call" in call and "com.superlogical.terminal.format" in call for call in cli.calls
    )


@pytest.mark.parametrize("surface", ["capability", "read", "eligible"])
async def test_boolean_uid_is_not_foreground_owner(cli, monkeypatch, surface):
    """pins_existing: bool must not acquire authority through its equality with uid 1."""
    from answerback_bridge.rex import Rex
    from answerback_bridge.rex_service import RexService

    monkeypatch.setattr("answerback_bridge.rex.os.getuid", lambda: 1)
    cli.foreground = {**cli.foreground, "user_id": True}
    adapter = Rex("/fixture/rex", server=ENDPOINT)
    if surface == "read":
        with pytest.raises(BridgeError, match="local bounded-read capability"):
            await adapter.read(S, B)
        assert not any(
            "call" in call and "com.superlogical.terminal.format" in call for call in cli.calls
        )
    else:
        observation = await adapter.terminal(S, B)
        if surface == "capability":
            assert observation["capabilities"] == {"read": False, "enroll": False}
        else:
            with pytest.raises(ValueError, match="owned by this OS user"):
                RexService.eligible(observation)


async def test_read_without_foreground_is_refused(cli):
    """pins_existing: no foreground record means no read authority."""
    from answerback_bridge.rex import Rex

    cli.foreground = None
    with pytest.raises(BridgeError, match="local bounded-read capability"):
        await Rex("/fixture/rex", server=ENDPOINT).read(S, B)
    assert not any(
        "call" in call and "com.superlogical.terminal.format" in call for call in cli.calls
    )


@pytest.mark.parametrize("cwd", [None, "", "relative/project"])
async def test_owned_read_does_not_require_enrollment_cwd(cli, cwd):
    """pins_existing: owned reads do not require enrollment's absolute cwd."""
    from answerback_bridge.rex import Rex

    cli.foreground = {**cli.foreground, "cwd": cwd}
    adapter = Rex("/fixture/rex", server=ENDPOINT)
    assert (await adapter.terminal(S, B))["capabilities"] == {"read": True, "enroll": False}
    assert (await adapter.read(S, B))["text"] == "é"


async def test_inventory_partial_and_missing_process(cli):
    from answerback_bridge.rex import Rex

    cli.foreground = None
    result = await Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert result["peers"][0]["capabilities"] == {"read": False, "enroll": False}
    cli.overrides = {"session.list_blocks": BridgeError("unavailable")}
    result = await Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not result["complete"] and result["errors"]


@pytest.mark.parametrize(
    "capture", [{"content": "é", "id": 7, "total": 100}, {"content": "", "id": 7, "total": 100}]
)
async def test_bounded_prefix_no_blob_fetch(cli, capture):
    from answerback_bridge.rex import Rex

    cli.capture = capture
    result = await Rex("/fixture/rex", server=ENDPOINT).read(S, B, max_bytes=4)
    assert result["capture_kind"] == "bounded_prefix" and result["truncated"]
    assert result["blob_id"] == 7
    assert result["bytes_returned"] == len(capture["content"].encode())
    assert not any("blob_read" in c for c in cli.calls)


@pytest.mark.parametrize(
    "capture",
    [
        {"content": "too long"},
        {"content": "é", "total": 1},
        {"content": "a", "id": 0},
        {"content": "a", "id": 7, "eof": True},
        {"content": None},
        {"content": "a", "total": 100, "eof": True},
    ],
)
async def test_malformed_capture_rejected(cli, capture):
    from answerback_bridge.rex import Rex

    cli.capture = capture
    with pytest.raises(BridgeError):
        await Rex("/fixture/rex", server=ENDPOINT).read(S, B, max_bytes=4)


@pytest.mark.parametrize("limit", [0, -1, True, 1048577])
async def test_capture_limits_positive_and_bounded(cli, limit):
    from answerback_bridge.rex import Rex

    with pytest.raises(ValueError):
        await Rex("/fixture/rex", server=ENDPOINT).read(S, B, max_bytes=limit)


async def test_inventory_truncation_and_other_session_survives(cli):
    from answerback_bridge.rex import MAX_SESSIONS, Rex

    cli.overrides = {
        "session.list": {"sessions": [{"session_id": "bad"}, *[{"session_id": S}] * MAX_SESSIONS]}
    }
    result = await Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert result["peers"] and not result["complete"]
    assert any("truncated" in e["error"] for e in result["errors"])


@pytest.mark.parametrize(
    "method,result",
    [
        ("session.list", {"sessions": "bad"}),
        ("session.list_blocks", {"blocks": [None]}),
        ("com.superlogical.terminal.process", {"foreground": {"pid": True, "name": "shell"}}),
        ("com.superlogical.terminal.program_status", {"records": "bad"}),
    ],
)
async def test_malformed_metadata_marks_inventory_incomplete(cli, method, result):
    from answerback_bridge.rex import Rex

    cli.overrides = {method: result}
    inventory = await Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not inventory["complete"]
    assert inventory["errors"]


async def test_missing_status_preserves_enrollment(cli):
    from answerback_bridge.rex import Rex

    cli.missing = {"com.superlogical.terminal.program_status"}
    result = await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert result["capabilities"]["enroll"]
    assert result["reported_status"] == []


async def test_expired_snapshot_fails_without_retry(cli):
    from answerback_bridge.rex import Rex

    cli.overrides = {"com.superlogical.terminal.format": BridgeError("not_found expired")}
    with pytest.raises(BridgeError, match="expired"):
        await Rex("/fixture/rex", server=ENDPOINT).read(S, B)
    assert sum("call" in c and "com.superlogical.terminal.format" in c for c in cli.calls) == 1


@pytest.mark.parametrize("method", ["session.list", "session.list_blocks"])
async def test_missing_collection_is_not_empty_inventory(cli, method):
    from answerback_bridge.rex import Rex

    cli.overrides = {method: {}}
    result = await Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not result["complete"]


async def test_missing_foreground_observation_is_not_enrollment(cli):
    from answerback_bridge.rex import Rex

    cli.overrides = {"com.superlogical.terminal.process": {}}
    result = await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not result["capabilities"]["enroll"]


async def test_malformed_schema_disables_dependent_capability(cli, monkeypatch):
    from answerback_bridge import rex

    original = cli.run

    async def malformed(argv, **kwargs):
        output = await original(argv, **kwargs)
        if "describe" in argv and "com.superlogical.terminal.format" in argv:
            value = json.loads(output.stdout)
            value["args_schema"] = {
                "properties": {"max_bytes": {"type": "string"}, "format": {"type": "string"}}
            }
            return Output(json.dumps(value), "")
        return output

    monkeypatch.setattr(rex, "run", malformed)
    assert not (await rex.Rex("/fixture/rex", server=ENDPOINT).probe())["capabilities"]["read"]


@pytest.mark.parametrize(
    "field,value", [("name", ""), ("cwd", ["/project"]), ("path", 42), ("script", {})]
)
async def test_malformed_foreground_strings_rejected(cli, field, value):
    from answerback_bridge.rex import Rex

    cli.foreground = {**cli.foreground, field: value}
    with pytest.raises(BridgeError, match="foreground"):
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)


@pytest.mark.parametrize("cwd", [None, "", "relative/project"])
async def test_enrollment_requires_absolute_nonempty_cwd(cli, cwd):
    from answerback_bridge.rex import Rex

    cli.foreground = {**cli.foreground, "cwd": cwd}
    assert not (await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B))["capabilities"]["enroll"]


@pytest.mark.parametrize("invoke", [None, [], "method"])
async def test_malformed_optional_invoke_preserves_discovery(cli, monkeypatch, invoke):
    from answerback_bridge import rex

    original = cli.run

    async def malformed(argv, **kwargs):
        output = await original(argv, **kwargs)
        if "describe" in argv and rex.TERMINAL + ".format" in argv:
            value = json.loads(output.stdout)
            value["invoke"] = invoke
            return Output(json.dumps(value), "")
        return output

    monkeypatch.setattr(rex, "run", malformed)
    result = await rex.Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert result["complete"] and len(result["peers"]) == 1
    assert result["peers"][0]["capabilities"] == {"read": False, "enroll": True}


@pytest.mark.parametrize("state", [[], {}, None, 42])
async def test_malformed_status_preserves_other_terminal(cli, monkeypatch, state):
    from answerback_bridge import rex

    original = cli.run
    cli.overrides = {"session.list_blocks": {"blocks": [cli.block, cli.block]}}
    status_calls = 0

    async def malformed(argv, **kwargs):
        nonlocal status_calls
        output = await original(argv, **kwargs)
        if "call" in argv and rex.TERMINAL + ".program_status" in argv:
            status_calls += 1
            if status_calls == 1:
                return Output(json.dumps({"records": [{"state": state}]}), "")
        return output

    monkeypatch.setattr(rex, "run", malformed)
    result = await rex.Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not result["complete"] and len(result["peers"]) == 1
    assert "status" in result["errors"][0]["error"]


async def test_inventory_total_deadline_returns_collected_peers(cli, monkeypatch):
    import asyncio

    from answerback_bridge import rex

    monkeypatch.setattr(rex, "OPERATION_SECONDS", 0.02, raising=False)
    original = cli.run
    cli.overrides = {"session.list_blocks": {"blocks": [cli.block, cli.block]}}
    described = 0

    async def delayed(argv, **kwargs):
        nonlocal described
        if "call" in argv and "session.describe_block" in argv:
            described += 1
            if described == 2:
                await asyncio.sleep(0.1)
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    result = await rex.Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not result["complete"] and len(result["peers"]) == 1
    assert "deadline" in result["errors"][-1]["error"]


@pytest.mark.parametrize("operation", ["probe", "terminal", "read"])
async def test_complete_operation_budget_includes_cold_probe(cli, monkeypatch, operation):
    import asyncio

    from answerback_bridge import rex

    monkeypatch.setattr(rex, "OPERATION_SECONDS", 0.02, raising=False)
    original = cli.run

    async def delayed(argv, **kwargs):
        await asyncio.sleep(0.006)
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    adapter = rex.Rex("/fixture/rex", server=ENDPOINT)
    with pytest.raises(TimeoutError):
        await getattr(adapter, operation)(*(() if operation == "probe" else (S, B)))


async def test_operation_reserves_bounded_cleanup_and_preserves_peers(cli, monkeypatch):
    import asyncio

    from answerback_bridge import rex

    monkeypatch.setattr(rex, "OPERATION_SECONDS", 0.2)
    cli.overrides = {"session.list_blocks": {"blocks": [cli.block, cli.block, cli.block]}}
    original = cli.run
    original_timeout = asyncio.timeout
    budgets = []

    def timeout(seconds):
        budgets.append(seconds)
        return original_timeout(seconds)

    monkeypatch.setattr(rex.asyncio, "timeout", timeout)
    described = 0
    cleanup_finished = False

    async def delayed(argv, **kwargs):
        nonlocal described, cleanup_finished
        assert 0 < kwargs["cleanup_seconds"] <= rex.OPERATION_SECONDS * 0.05
        if "call" in argv and "session.describe_block" in argv:
            described += 1
            if described == 2:
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(kwargs["cleanup_seconds"])
                    cleanup_finished = True
                    raise CommandCleanupError("Command cleanup failed to reap owned child")
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    result = await rex.Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert cleanup_finished
    assert described == 2
    assert budgets == [rex.OPERATION_SECONDS * 0.9]
    assert len(result["peers"]) == 1 and not result["complete"]
    assert any("cleanup" in error["error"] for error in result["errors"])


@pytest.mark.parametrize("listing", [{"sessions": []}, {"sessions": None}])
async def test_failed_lookup_confirmed_session_absence(cli, listing):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {
        "session.describe_block": BridgeError("arbitrary failure"),
        "session.list": listing,
    }
    with pytest.raises(RexIdentityChanged, match="absent"):
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)


@pytest.mark.parametrize(
    "listing",
    [
        {},
        {"blocks": "bad"},
        {"blocks": [None]},
        {"blocks": [], "truncated": True},
        BridgeError("inaccessible"),
        {"blocks": [{"block_id": B}]},
    ],
)
async def test_failed_lookup_uncertain_listing_is_not_identity_change(cli, listing):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {
        "session.describe_block": BridgeError("lookup failed"),
        "session.list_blocks": listing,
    }
    with pytest.raises(BridgeError) as caught:
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not isinstance(caught.value, RexIdentityChanged)


async def test_failed_lookup_confirmed_block_absence(cli):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {
        "session.describe_block": BridgeError("lookup failed"),
        "session.list_blocks": {"blocks": []},
    }
    with pytest.raises(RexIdentityChanged, match="absent"):
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)


async def test_missing_block_response_is_uncertain(cli):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {"session.describe_block": {}}
    with pytest.raises(BridgeError) as caught:
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not isinstance(caught.value, RexIdentityChanged)


@pytest.mark.parametrize("returned_id", ["suffix", "", "block:not-a-uuid"])
async def test_malformed_string_block_identity_is_uncertain(cli, returned_id):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {"session.describe_block": {"block": {**cli.block, "block_id": returned_id}}}
    with pytest.raises(BridgeError) as caught:
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not isinstance(caught.value, RexIdentityChanged)


@pytest.mark.parametrize(
    "listing",
    [
        {"sessions": [{"session_id": S}] * 33},
        {"sessions": [{"session_id": "suffix"}]},
        {"sessions": [], "next_cursor": "more"},
        BridgeError("offline"),
    ],
)
async def test_failed_lookup_unbounded_session_evidence_is_uncertain(cli, listing):
    from answerback_bridge.rex import Rex, RexIdentityChanged

    cli.overrides = {"session.describe_block": BridgeError("failed"), "session.list": listing}
    with pytest.raises(BridgeError) as caught:
        await Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not isinstance(caught.value, RexIdentityChanged)


async def test_absence_collection_server_restart_remains_uncertain(cli, monkeypatch):
    from answerback_bridge import rex

    original = cli.run
    cli.overrides = {
        "session.describe_block": BridgeError("failed"),
        "session.list": {"sessions": []},
    }

    async def restarted(argv, **kwargs):
        output = await original(argv, **kwargs)
        if "call" in argv and "session.list" in argv:
            cli.identity = {**cli.identity, "instance_id": "replacement"}
        return output

    monkeypatch.setattr(rex, "run", restarted)
    with pytest.raises(BridgeError) as caught:
        await rex.Rex("/fixture/rex", server=ENDPOINT).terminal(S, B)
    assert not isinstance(caught.value, rex.RexIdentityChanged)


@pytest.mark.parametrize(
    "field,value,concurrent_probe",
    [
        ("instance_id", "replacement", False),
        ("instance_id", "replacement", True),
        ("host_id", "other", False),
        ("endpoint", "unix:///tmp/other", False),
    ],
)
async def test_format_phase_restart_refuses_content(
    cli, monkeypatch, field, value, concurrent_probe
):
    from answerback_bridge import rex

    original = cli.run
    adapter = rex.Rex("/fixture/rex", server=ENDPOINT)

    async def restarted(argv, **kwargs):
        output = await original(argv, **kwargs)
        if "call" in argv and rex.TERMINAL + ".format" in argv:
            cli.identity = {**cli.identity, field: value}
            if concurrent_probe:
                await adapter.probe()
        return output

    monkeypatch.setattr(rex, "run", restarted)
    with pytest.raises(BridgeError, match="identity"):
        await adapter.read(S, B, max_bytes=4)


async def test_successful_inventory_restart_refuses_crossed_rows(cli, monkeypatch):
    from answerback_bridge import rex

    original = cli.run

    async def restarted(argv, **kwargs):
        if "call" in argv and "session.describe_block" in argv:
            cli.identity = {**cli.identity, "instance_id": "replacement"}
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", restarted)
    result = await rex.Rex("/fixture/rex", server=ENDPOINT).inventory()
    assert not result["complete"]
    assert result["peers"] == []


@pytest.mark.parametrize("operation", ["probe", "terminal", "read"])
async def test_restart_during_capability_collection_refuses_observation(
    cli, monkeypatch, operation
):
    from answerback_bridge import rex

    original = cli.run

    async def restarted(argv, **kwargs):
        output = await original(argv, **kwargs)
        if "describe" in argv and rex.TERMINAL + ".format" in argv:
            cli.identity = {**cli.identity, "instance_id": "replacement"}
        return output

    monkeypatch.setattr(rex, "run", restarted)
    with pytest.raises(BridgeError, match="identity"):
        await getattr(rex.Rex("/fixture/rex", server=ENDPOINT), operation)(
            *(() if operation == "probe" else (S, B))
        )


async def test_read_keeps_operation_capabilities_during_concurrent_probe(cli, monkeypatch):
    import asyncio

    from answerback_bridge import rex

    original = cli.run
    adapter = rex.Rex("/fixture/rex", server=ENDPOINT)
    probe_started, release_probe = asyncio.Event(), asyncio.Event()
    read_started, release_read = asyncio.Event(), asyncio.Event()

    async def delayed(argv, **kwargs):
        if "describe" in argv and rex.TERMINAL + ".format" in argv:
            if asyncio.current_task() is probe:
                probe_started.set()
                await release_probe.wait()
                raise BridgeError("format capability unavailable to older probe")
        if "call" in argv and "session.describe_block" in argv:
            read_started.set()
            await release_read.wait()
        return await original(argv, **kwargs)

    monkeypatch.setattr(rex, "run", delayed)
    probe = asyncio.create_task(adapter.probe())
    read = None
    try:
        await asyncio.wait_for(probe_started.wait(), 1)
        read = asyncio.create_task(adapter.read(S, B, max_bytes=4))
        await asyncio.wait_for(read_started.wait(), 1)
        release_probe.set()
        assert not (await asyncio.wait_for(probe, 1))["capabilities"]["read"]
        release_read.set()
        result = await asyncio.wait_for(read, 1)
        assert result["text"] == "é"
        assert result["binding"]["server_instance_id"] == "run-1"
    finally:
        release_probe.set()
        release_read.set()
        await asyncio.gather(probe, *([read] if read is not None else []), return_exceptions=True)
