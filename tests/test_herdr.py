import sys
from pathlib import Path

from codex_herdr_bridge.config import Config
from codex_herdr_bridge.herdr import Herdr


async def test_agent_read_preserves_plain_terminal_output(tmp_path: Path) -> None:
    fake_cli = tmp_path / "herdr"
    fake_cli.write_text(f"#!{sys.executable}\nprint('A plain terminal response, not JSON')\n")
    fake_cli.chmod(0o700)
    result = await Herdr(Config("test-session", str(fake_cli))).read("reviewer")
    assert result["text"] == "A plain terminal response, not JSON\n"
    assert result["source"] == "recent-unwrapped"
