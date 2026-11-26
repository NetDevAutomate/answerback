"""T-226 item 7: what leaves this machine for a remote terminal carries no credential.

The remote envelope, every `hc` argv of a remote delivery, and the placement probe
argv name none of the provider secret variables, no 1Password reference and no
`--env` (Core forwards an environment to another machine only through `--env`).
`public_task` keeps hiding the result token and path that name a result file.

The bridge process here runs with the secret variables set, as a launch shell
that exported them would, to show that nothing copies them into an argv.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    delivered,
    make_bridge,
    make_fake,
)

from answerback_bridge.config import public_task
from answerback_bridge.placement import probe
from answerback_bridge.providers.herdr.core_tasks import CoreTasks
from answerback_bridge.service import Bridge
from answerback_bridge.tasks import TaskStore

NAMES = ("AWS_BEARER_TOKEN_BEDROCK", "CODE_HARNESS_BEDROCK_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN")
# A value that would be recognisable anywhere it was copied to.
CANARY = "canary-" + "7f3a" * 8


def assert_clean(text: str) -> None:
    for word in (*NAMES, "op://", "--env", CANARY):
        assert word not in text, word


@pytest.fixture(autouse=True)
def secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        monkeypatch.setenv(name, CANARY)


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


def test_remote_and_local_envelopes_name_no_secret(tmp_path: Path) -> None:
    tasks = CoreTasks(TaskStore(tmp_path / "tasks.sqlite"), tmp_path / "config.json")
    task_id = "00000000-0000-0000-0000-000000000000"
    remote = tasks.envelope(task_id, "Review the parser", token="0" * 32)
    local = tasks.envelope(task_id, "Review the parser")
    assert "answerback-result" in remote
    assert_clean(remote)
    assert_clean(local)


async def test_every_hc_argv_of_a_remote_delivery_is_clean(fake: FakeCore, tmp_path: Path) -> None:
    bridge: Bridge = make_bridge(fake, tmp_path)
    try:
        task = await delivered(bridge, REMOTE, "Review the parser", "clean-1")
        assert task["status"] == "submitted"
        calls = fake.calls()
        assert ["machine", "ls", "--json"] in calls
        assert any(args[0] == "send" for args in calls)
        for args in calls:
            assert_clean(json.dumps(args))
    finally:
        await bridge.close()


@pytest.mark.parametrize("machine", [probe.LOCAL, MACHINE])
async def test_probe_argv_is_clean(machine: str) -> None:
    seen: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, bytes]:
        seen.append(argv)
        return 0, b"probe_end=1\n"

    await probe.read([machine], runner=runner, hc_binary="hc")
    (argv,) = seen
    if machine != probe.LOCAL:
        assert argv[:4] == ["hc", "exec", "--on", MACHINE]
    assert_clean(json.dumps(argv))


async def test_public_views_drop_the_result_token_and_path(fake: FakeCore, tmp_path: Path) -> None:
    bridge = make_bridge(fake, tmp_path)
    try:
        task = await delivered(bridge, REMOTE, "Review the parser", "clean-2")
        token = task["metadata"]["result_token"]
        stored: dict[str, Any] = {**task, "metadata": {**task["metadata"], "result_path": "p"}}
        shown = public_task(stored)
        assert "result_token" not in shown["metadata"]
        assert "result_path" not in shown["metadata"]
        assert token not in json.dumps(shown)
        assert token not in json.dumps(await bridge.get_task(task["id"]))
    finally:
        await bridge.close()
