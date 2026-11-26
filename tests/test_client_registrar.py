"""Client installation is dispatched through provider registrar hooks."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_plugin import unbound_owners

from answerback_bridge import registration
from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.plugin import (
    ClientInstall,
    ClientMigration,
    ClientRegistrar,
    ClientRegistrationError,
)


class FakeRegistrar(ClientRegistrar):
    name = "fixture"

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, Path]] = []

    def install_path(self, home: Path, server_name: str, *, real_home: bool) -> Path:
        del server_name, real_home
        return home / ".fixture"

    def migration_path(self, home: Path, *, real_home: bool) -> Path:
        del real_home
        return home / ".fixture"

    def install(self, home: Path, **kwargs: Any) -> ClientInstall:
        self.calls.append(("install", home))
        if self.failure is not None:
            raise self.failure
        return ClientInstall(home / ".fixture")

    def migrate(self, home: Path, **kwargs: Any) -> ClientMigration:
        self.calls.append(("migrate", home))
        if self.failure is not None:
            raise self.failure
        return ClientMigration(home / ".fixture")


class ApplyFailureRegistrar(FakeRegistrar):
    def install(self, home: Path, **kwargs: Any) -> ClientInstall:
        self.calls.append(("install", home))
        return ClientInstall(home / ".fixture", operation=object())

    async def apply_install(self, plan: Any, server_name: str) -> None:
        del plan, server_name
        raise OSError("private install detail")

    def migrate(self, home: Path, **kwargs: Any) -> ClientMigration:
        self.calls.append(("migrate", home))
        return ClientMigration(home / ".fixture", change=True, operation=object())

    async def apply_migration(self, plan: Any) -> Any:
        del plan
        raise OSError("private migration detail")


class ProgrammingErrorRegistrar(FakeRegistrar):
    def __init__(self, phase: str, error: Exception) -> None:
        super().__init__()
        self.phase = phase
        self.error = error

    def install(self, home: Path, **kwargs: Any) -> ClientInstall:
        self.calls.append(("install", home))
        if self.phase == "install-preview":
            raise self.error
        return ClientInstall(
            home / ".fixture",
            operation=object() if self.phase == "install-apply" else None,
        )

    async def apply_install(self, plan: Any, server_name: str) -> None:
        del plan, server_name
        if self.phase == "install-apply":
            raise self.error

    def migrate(self, home: Path, **kwargs: Any) -> ClientMigration:
        self.calls.append(("migrate", home))
        if self.phase == "migrate-preview":
            raise self.error
        return ClientMigration(
            home / ".fixture",
            change=self.phase == "migrate-apply",
            operation=object() if self.phase == "migrate-apply" else None,
        )

    async def apply_migration(self, plan: Any) -> Any:
        del plan
        if self.phase == "migrate-apply":
            raise self.error
        return None


class RollbackProgrammingErrorRegistrar(FakeRegistrar):
    def migrate(self, home: Path, **kwargs: Any) -> ClientMigration:
        self.calls.append(("migrate", home))
        return ClientMigration(home / f".{self.name}", change=True, operation=object())

    async def apply_migration(self, plan: Any) -> Any:
        del plan
        return object()

    def rollback_migration(self, rollback: Any) -> None:
        del rollback
        raise AttributeError("rollback programming error")


class FakePlugin:
    def __init__(self, registrar: ClientRegistrar) -> None:
        self._registrar = registrar

    def registrar(self) -> ClientRegistrar:
        return self._registrar


def test_client_names_come_from_the_kernel_clients_and_registered_hooks() -> None:
    kernel = ("codex", "claude", "grok", "kiro", "crew")
    plugins = {
        plugin.definition.registration: plugin for plugin in builtin_plugins(unbound_owners())
    }
    registrars = {
        registrar.name: registrar
        for plugin in plugins.values()
        if (registrar := plugin.registrar()) is not None
    }
    assert registration.CLIENTS == (*kernel, *registrars)
    for name, registrar in registrars.items():
        assert registration.client_registrar(name) is registrar
    assert set(registrars).isdisjoint(kernel)

    registrar = FakeRegistrar()
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        assert registration.CLIENTS[-1] == "fixture"
        assert registration.client_registrar("fixture") is registrar
    finally:
        registration.unregister_client_registrar("fixture")
    assert "fixture" not in registration.CLIENTS


def test_codex_report_paths_use_only_path_arithmetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = "~answerback-user-that-does-not-exist"
    monkeypatch.setenv("CODEX_HOME", codex_home)

    def filesystem_access(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("report path calculation touched the filesystem")

    monkeypatch.setattr(Path, "expanduser", filesystem_access)
    registrar = registration.client_registrar("codex")
    expected = Path.cwd() / codex_home / "config.toml"

    assert registrar.install_path(tmp_path, "answerback_bridge", real_home=True) == expected
    assert registrar.migration_path(tmp_path, real_home=True) == expected


def test_client_registrar_has_no_unused_status_hook() -> None:
    assert "status" not in ClientRegistrar.__dict__
    assert "status" not in type(registration.client_registrar("codex")).__dict__


async def test_install_and_migrate_dispatch_through_the_registry(tmp_path: Path) -> None:
    registrar = FakeRegistrar()
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        installed = await registration.install_clients(
            tmp_path / "bridge.json",
            ["fixture"],
            home=tmp_path,
            executable="/bridge",
        )
        migrated = await registration.migrate_clients(
            tmp_path / "bridge.json", ["fixture"], home=tmp_path
        )
    finally:
        registration.unregister_client_registrar("fixture")

    assert installed["success"] and installed["clients"][0]["status"] == "unchanged"
    assert migrated["success"] and migrated["clients"][0]["status"] == "unchanged"
    assert registrar.calls == [
        ("install", tmp_path),
        ("migrate", tmp_path),
    ]


async def test_one_raising_registrar_is_reported_without_aborting_other_previews(
    tmp_path: Path,
) -> None:
    registrar = FakeRegistrar(failure=ClientRegistrationError("provider refusal"))
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        result = await registration.install_clients(
            tmp_path / "bridge.json",
            ["fixture", "codex"],
            home=tmp_path,
            executable="/bridge",
            apply=True,
        )
    finally:
        registration.unregister_client_registrar("fixture")

    assert not result["success"]
    assert result["clients"][0] == {
        "client": "fixture",
        "path": str(tmp_path / ".fixture"),
        "entry": {
            "command": "/bridge",
            "args": [
                "--config",
                str(tmp_path / "bridge.json"),
                "mcp",
                "--client",
                "fixture",
            ],
        },
        "ready": False,
        "would_change": False,
        "status": "refused",
        "error": "provider refusal",
    }
    assert result["clients"][1]["client"] == "codex"
    assert result["clients"][1]["status"] == "blocked"


async def test_refused_crew_install_keeps_inheritance_note(tmp_path: Path) -> None:
    path = tmp_path / ".kiro/settings/mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "answerback_bridge": {
                        "command": "/other/bridge",
                        "args": ["--config", "/other/config.json", "mcp"],
                    }
                }
            }
        )
    )

    result = await registration.install_clients(
        tmp_path / "bridge.json",
        ["crew"],
        home=tmp_path,
        executable="/bridge",
        apply=True,
    )

    assert not result["success"]
    assert result["clients"][0]["status"] == "refused"
    assert result["clients"][0]["note"] == (
        "Uses Kiro's global entry; Crew inheritance and reload need verification."
    )


async def test_refused_codex_install_reports_configured_real_home_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    destination = tmp_path / "codex-home" / "config.toml"
    destination.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(destination.parent))

    result = await registration.install_clients(
        tmp_path / "bridge.json", ["codex"], executable="/bridge"
    )

    assert result["clients"][0]["path"] == str(destination)
    assert result["clients"][0]["entry"] == {
        "command": "/bridge",
        "args": [
            "--config",
            str(tmp_path / "bridge.json"),
            "mcp",
            "--client",
            "codex",
        ],
    }


async def test_refused_codex_migration_reports_configured_real_home_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    destination = tmp_path / "codex-home" / "config.toml"
    destination.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(destination.parent))

    result = await registration.migrate_clients(tmp_path / "bridge.json", ["codex"])

    assert not result["success"]
    assert result["clients"][0]["status"] == "refused"
    assert result["clients"][0]["path"] == str(destination)


@pytest.mark.parametrize("action", ["install", "migrate"])
async def test_preview_oserror_uses_configuration_read_failure(tmp_path: Path, action: str) -> None:
    registrar = FakeRegistrar(failure=OSError("private preview detail"))
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        if action == "install":
            result = await registration.install_clients(
                tmp_path / "bridge.json",
                ["fixture"],
                home=tmp_path,
                executable="/bridge",
            )
        else:
            result = await registration.migrate_clients(
                tmp_path / "bridge.json", ["fixture"], home=tmp_path
            )
    finally:
        registration.unregister_client_registrar("fixture")

    assert result["clients"][0]["error"] == "Configuration could not be read"


async def test_install_apply_oserror_uses_configuration_write_failure(tmp_path: Path) -> None:
    registrar = ApplyFailureRegistrar()
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        result = await registration.install_clients(
            tmp_path / "bridge.json",
            ["fixture"],
            home=tmp_path,
            executable="/bridge",
            apply=True,
        )
    finally:
        registration.unregister_client_registrar("fixture")

    assert result["clients"][0]["error"] == "Configuration write failed"


async def test_migrate_apply_oserror_uses_migration_write_failure(tmp_path: Path) -> None:
    registrar = ApplyFailureRegistrar()
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        result = await registration.migrate_clients(
            tmp_path / "bridge.json", ["fixture"], home=tmp_path, apply=True
        )
    finally:
        registration.unregister_client_registrar("fixture")

    assert result["clients"][0]["error"] == "Migration write failed"


def test_native_skill_symlink_refusal_names_the_provider(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.symlink_to(tmp_path / "other", target_is_directory=True)
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(
        ClientRegistrationError,
        match="Fixture's native installer cannot safely replace a symlinked skill destination",
    ):
        registration._prepare_skill(  # noqa: SLF001 - compatibility text is kernel-owned
            target,
            source,
            registration._snapshot(source),  # noqa: SLF001
            True,
            tmp_path,
            native="/fixture",
            native_name="Fixture",
        )


async def test_migrate_refusal_is_reported_and_next_client_still_previews(
    tmp_path: Path,
) -> None:
    registrar = FakeRegistrar(failure=ClientRegistrationError("provider refusal"))
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        result = await registration.migrate_clients(
            tmp_path / "bridge.json", ["fixture", "codex"], home=tmp_path
        )
    finally:
        registration.unregister_client_registrar("fixture")

    assert not result["success"]
    assert result["clients"][0] == {
        "client": "fixture",
        "path": str(tmp_path / ".fixture"),
        "ready": False,
        "would_change": False,
        "status": "refused",
        "error": "provider refusal",
    }
    assert result["clients"][1]["client"] == "codex"
    assert result["clients"][1]["status"] == "unchanged"
    assert registrar.calls == [("migrate", tmp_path)]


@pytest.mark.parametrize("error_type", [AttributeError, KeyError])
@pytest.mark.parametrize(
    "phase",
    ["install-preview", "install-apply", "migrate-preview", "migrate-apply"],
)
async def test_registrar_programming_errors_propagate(
    tmp_path: Path, phase: str, error_type: type[Exception]
) -> None:
    registrar = ProgrammingErrorRegistrar(phase, error_type("programming error"))
    registration.register_client_plugin(FakePlugin(registrar))  # type: ignore[arg-type]
    try:
        with pytest.raises(error_type, match="programming error"):
            if phase.startswith("install"):
                await registration.install_clients(
                    tmp_path / "bridge.json",
                    ["fixture"],
                    home=tmp_path,
                    executable="/bridge",
                    apply=phase.endswith("apply"),
                )
            else:
                await registration.migrate_clients(
                    tmp_path / "bridge.json",
                    ["fixture"],
                    home=tmp_path,
                    apply=phase.endswith("apply"),
                )
    finally:
        registration.unregister_client_registrar("fixture")


async def test_native_rollback_programming_error_propagates(tmp_path: Path) -> None:
    first = RollbackProgrammingErrorRegistrar()
    first.name = "first"
    second = ApplyFailureRegistrar()
    second.name = "second"
    registration.register_client_plugin(FakePlugin(first))  # type: ignore[arg-type]
    registration.register_client_plugin(FakePlugin(second))  # type: ignore[arg-type]
    try:
        with pytest.raises(AttributeError, match="rollback programming error"):
            await registration.migrate_clients(
                tmp_path / "bridge.json",
                ["first", "second"],
                home=tmp_path,
                apply=True,
            )
    finally:
        registration.unregister_client_registrar("second")
        registration.unregister_client_registrar("first")
