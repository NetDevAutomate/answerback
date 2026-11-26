"""T-226 item 5: guards that run before a Core receipt is reserved.

(a) A brief that holds a provider credential shape is refused for every hc target,
with one fixed sentence and no receipt. (b) A remote terminal's current directory
must be inside its machine's placement project folder when `hc_placement_policy`
names one; otherwise the send is allowed and the receipt records
`project_roots: unconfigured`.

The fake `hc` is the one of `test_core_result_file` (Herdr core 0.1.21 samples).
Credential-shaped strings are built by concatenation so that no literal token sits
in this file.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from test_core_result_file import (
    MACHINE,
    REMOTE,
    FakeCore,
    binding_of,
    delivered,
    make_fake,
    row,
)

from answerback_bridge.config import Config
from answerback_bridge.errors import public_bridge_error
from answerback_bridge.providers.herdr.core_tasks import (
    OUTSIDE_ROOTS,
    POLICY_INVALID,
    POLICY_UNREADABLE,
    SHAPE_REFUSAL,
)
from answerback_bridge.service import Bridge

# The remote terminal's foreground cwd in `test_core_result_file.row`.
CWD = "/home/user/code/project"

SECRETS = [
    "export AWS_BEARER_TOKEN_" + "BEDROCK=abc",
    'env = {"CODE_HARNESS_BEDROCK_' + 'TOKEN": "x"}',
    "OP_SERVICE_ACCOUNT_" + "TOKEN = 'x'",
    "read it from op:" + "//Private/bedrock/credential",
    "key " + "AB" + "SK" + "Q" * 40 + " here",
    "token " + "op" + "s_" + "eyJ" + "a" * 40,
]
HARMLESS = [
    "unset AWS_BEARER_TOKEN_" + "BEDROCK before the run",
    "The ABSK prefix marks a Bedrock key; never paste one.",
    "Ask the ops_team channel.",
    "Use the 1Password op CLI: op read.",
]


@pytest.fixture
def fake(tmp_path: Path) -> FakeCore:
    return make_fake(tmp_path)


def bridge_with(fake: FakeCore, tmp_path: Path, policy: str | None = None) -> Bridge:
    path = tmp_path / "config.json"
    Config(
        hc_enabled=True,
        codex_enabled=False,
        hc_binary=str(fake.binary),
        hc_send_machines=[MACHINE],
        hc_placement_policy=policy,
    ).save(path)
    return Bridge(Config.load(path), path)


def write_policy(tmp_path: Path, machines: dict[str, Any]) -> str:
    path = tmp_path / "routing.json"
    path.write_text(json.dumps({"placement": {"machines": machines}}))
    return str(path)


async def submit(bridge: Bridge, target: str, message: str, request: str) -> dict[str, Any]:
    return await bridge.peer_submit(
        f"hc:{target}", message, request, binding=binding_of(bridge, target)
    )


# (a) provider credential shapes.


@pytest.mark.parametrize("target", ["t1", REMOTE])
@pytest.mark.parametrize("secret", SECRETS)
async def test_brief_with_a_secret_shape_is_refused_without_a_receipt(
    fake: FakeCore, tmp_path: Path, target: str, secret: str
) -> None:
    bridge = bridge_with(fake, tmp_path)
    try:
        with pytest.raises(ValueError) as refused:
            await submit(bridge, target, f"Review this.\n{secret}\n", "secret-1")
        assert str(refused.value) == SHAPE_REFUSAL
        assert secret not in str(refused.value)
        assert bridge.store.by_request("secret-1") is None
        assert bridge.store.recent() == []
        assert fake.sends() == []
    finally:
        await bridge.close()


@pytest.mark.parametrize("message", HARMLESS)
async def test_mentions_without_a_credential_are_sent(
    fake: FakeCore, tmp_path: Path, message: str
) -> None:
    bridge = bridge_with(fake, tmp_path)
    try:
        task = await delivered(bridge, REMOTE, message, "harmless")
        assert task["status"] == "submitted"
        assert len(fake.sends()) == 1
    finally:
        await bridge.close()


def test_secret_refusal_has_a_public_code() -> None:
    public = public_bridge_error(ValueError(SHAPE_REFUSAL))
    assert public is not None and public.code == "brief_secret_shape"
    for text in (OUTSIDE_ROOTS, POLICY_UNREADABLE, POLICY_INVALID):
        assert public_bridge_error(ValueError(text)) is not None


# (b) project roots for remote terminals.


async def test_no_policy_allows_and_records_unconfigured(fake: FakeCore, tmp_path: Path) -> None:
    bridge = bridge_with(fake, tmp_path)
    try:
        task = await delivered(bridge, REMOTE, "Review", "roots-none")
        assert task["status"] == "submitted"
        assert task["metadata"]["project_roots"] == "unconfigured"
        local = await delivered(bridge, "t1", "Review", "roots-local")
        assert "project_roots" not in local["metadata"]
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    "machines",
    [
        {},
        {"example-host": {"project": "/elsewhere"}},
        {MACHINE: {"max_total": 2}},
        {MACHINE: {"project": None}},
    ],
)
async def test_a_machine_without_a_project_folder_is_unconfigured(
    fake: FakeCore, tmp_path: Path, machines: dict[str, Any]
) -> None:
    bridge = bridge_with(fake, tmp_path, write_policy(tmp_path, machines))
    try:
        task = await delivered(bridge, REMOTE, "Review", "roots-unset")
        assert task["metadata"]["project_roots"] == "unconfigured"
        assert len(fake.sends()) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("root", ["/home/user/code", CWD, CWD + "/"])
async def test_directory_inside_the_project_folder_is_sent_and_enforced(
    fake: FakeCore, tmp_path: Path, root: str
) -> None:
    bridge = bridge_with(fake, tmp_path, write_policy(tmp_path, {MACHINE: {"project": root}}))
    try:
        task = await delivered(bridge, REMOTE, "Review", "roots-in")
        assert task["status"] == "submitted"
        assert task["metadata"]["project_roots"] == "enforced"
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    ("root", "cwd"),
    [
        ("/home/user/other", CWD),
        # A shared prefix of characters is not a parent folder.
        ("/home/user/code/proj", CWD),
        ("/home/user/code", "/home/user/code/../secrets"),
        # A root with `..` is refused earlier, at parse (test below; review N2).
    ],
)
async def test_directory_outside_the_project_folder_is_refused_without_a_receipt(
    fake: FakeCore, tmp_path: Path, root: str, cwd: str
) -> None:
    terminal = row(REMOTE)
    terminal["foreground"]["cwd"] = cwd
    fake.update(terminals={"t1": row("t1"), REMOTE: terminal})
    bridge = bridge_with(fake, tmp_path, write_policy(tmp_path, {MACHINE: {"project": root}}))
    try:
        with pytest.raises(ValueError) as refused:
            await submit(bridge, REMOTE, "Review", "roots-out")
        assert str(refused.value) == OUTSIDE_ROOTS
        assert bridge.store.by_request("roots-out") is None
        assert fake.sends() == []
    finally:
        await bridge.close()


async def test_local_terminal_ignores_the_policy(fake: FakeCore, tmp_path: Path) -> None:
    bridge = bridge_with(fake, tmp_path, write_policy(tmp_path, {MACHINE: {"project": "/nowhere"}}))
    try:
        task = await delivered(bridge, "t1", "Review", "roots-local-only")
        assert task["status"] == "submitted"
        assert "project_roots" not in task["metadata"]
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        (None, POLICY_UNREADABLE),
        ("{not json", POLICY_INVALID),
        ('{"placement": {"order": ["x"]}}', POLICY_INVALID),
    ],
)
async def test_configured_policy_that_cannot_be_used_refuses_remote_sends(
    fake: FakeCore, tmp_path: Path, content: str | None, refusal: str
) -> None:
    path = tmp_path / "routing.json"
    if content is not None:
        path.write_text(content)
    bridge = bridge_with(fake, tmp_path, str(path))
    try:
        with pytest.raises(ValueError) as refused:
            await submit(bridge, REMOTE, "Review", "roots-broken")
        assert str(refused.value) == refusal
        assert bridge.store.by_request("roots-broken") is None
        assert fake.sends() == []
    finally:
        await bridge.close()


@pytest.mark.parametrize(
    "project",
    ["/", "//", "///", "/home/user/code/../code/project", "/home/user/..", "/../etc"],
)
async def test_policy_project_of_root_or_with_dotdot_is_refused_at_parse(
    fake: FakeCore, tmp_path: Path, project: str
) -> None:
    """Review F3 and N2: `/` would enforce nothing, `..` gave the wrong diagnostic.

    Both are refused when the policy is parsed, as `placement_policy_invalid`, before
    any receipt or send; the terminal in `CWD` is never weighed against them.
    """
    bridge = bridge_with(fake, tmp_path, write_policy(tmp_path, {MACHINE: {"project": project}}))
    try:
        with pytest.raises(ValueError) as refused:
            await submit(bridge, REMOTE, "Review", "roots-bad-project")
        assert str(refused.value) == POLICY_INVALID
        public = public_bridge_error(refused.value)
        assert public is not None and public.code == "placement_policy_invalid"
        assert bridge.store.by_request("roots-bad-project") is None
        assert fake.sends() == []
    finally:
        await bridge.close()


def test_placement_parser_refuses_root_and_dotdot_projects() -> None:
    from answerback_bridge.placement.facts import FactsError, parse_facts

    for project in ("/", "//", "/a/../b", "/a/.."):
        with pytest.raises(FactsError, match="below `/` without `..`"):
            parse_facts({"placement": {"machines": {"nuc01": {"project": project}}}})
    facts = parse_facts({"placement": {"machines": {"nuc01": {"project": "/a/./b/"}}}})
    assert facts.machines["nuc01"].project == "/a/./b/"


def test_policy_path_must_be_absolute_and_is_omitted_at_default(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        Config(hc_placement_policy="status/routing.json")
    path = tmp_path / "config.json"
    Config(provider_selection=[]).save(path)
    assert "hc_placement_policy" not in json.loads(path.read_text())
    Config(provider_selection=[], hc_placement_policy="/etc/routing.json").save(path)
    assert Config.load(path).hc_placement_policy == "/etc/routing.json"
