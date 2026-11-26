"""Record what every public config field reads for each redacted live config.

Run against the package under test (``PYTHONPATH=src python tests/fixtures/live-configs/
snapshot_fields.py``): for every ``<host>.json`` beside this script it writes
``expected/<host>.json`` holding each flat field's value as ``Config`` reads it and prints
the config's ``routing_digest`` (kept as a literal in the test, where the secret scanner's
allowlist pragma can mark it). The committed ``expected/`` files were produced by the
pre-split ``main`` loader (``src`` at ab693a31, the parent of the config split, T-198) run
over the neutralised fixtures beside this script; ``tests/test_config_migration.py``
compares the split loader against them, so the oracle is the pre-split reading, not this
script's output. The fixtures hold the kernel's fields only (host names and home paths
neutralised); the internal composition's copies with the plugin's block are the plugin's.

The field list is spelled out rather than taken from ``dataclasses.fields(Config)`` because
after the split the flat names are compatibility properties, not fields; the script must
read the same names on both sides of the change.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

KERNEL_FIELDS = (
    "provider_selection",
    "client_registrations",
    "heartbeat_interval",
    "heartbeat_misses",
)
FLAT_FIELDS = (
    "herdr_session",
    "herdr_binary",
    "codex_binary",
    "desktop_threads",
    "codex_home",
    "codex_enabled",
    "kiro_binary",
    "kiro_workspaces",
    "kiro_acp_engine",
    "kiro_trust_all_tools",
    "kiro_trust_tools",
    "kiro_active_budget",
    "crew_binary",
    "crew_enabled",
    "crew_port",
    "hc_enabled",
    "hc_binary",
    "hc_session",
    "hc_send_to_done",
    "hc_send_machines",
    "hc_events",
    "hc_placement_policy",
    "rex_enabled",
    "rex_binary",
    "rex_server",
    "rex_host_id",
    *(
        f"{provider}_{name}"
        for provider in ("codex_cli", "claude_code", "grok")
        for name in ("binary", "home", "workspaces", "active_budget")
    ),
)


def snapshot(path: Path) -> dict[str, object]:
    from answerback_bridge.config import Config, routing_digest

    config = Config.load(path)
    fields = {name: getattr(config, name) for name in (*KERNEL_FIELDS, *FLAT_FIELDS)}
    return {
        "fields": fields,
        "registered_providers": config.registered_providers(),
        "routing_digest": routing_digest(config),
    }


def main() -> int:
    out = HERE / "expected"
    out.mkdir(exist_ok=True)
    for path in sorted(HERE.glob("*.json")):
        record = snapshot(path)
        digest = record.pop("routing_digest")
        (out / path.name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(f"{path.name}: {out / path.name} routing_digest={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
