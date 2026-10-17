import re
from pathlib import Path

from typer.main import get_command

from localdash.cli import app

SKILL = Path(__file__).resolve().parents[1] / "skill" / "SKILL.md"


def test_skill_frontmatter():
    text = SKILL.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    assert re.search(r"^name: localdash$", m.group(1), re.M)
    assert re.search(r"^description: .{40,}", m.group(1), re.M)


def test_skill_only_uses_real_commands():
    commands = set(get_command(app).commands)
    used = set(re.findall(r"\blocaldash ([a-z][a-z-]+)", SKILL.read_text()))
    used -= {"runs", "is"}  # prose, not commands
    assert {"schema", "validate", "render"} <= used
    assert used <= commands, used - commands
