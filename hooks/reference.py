"""MkDocs hook: generate a reference page for every reusable workflow and composite action.

The pages are built from the YAML files themselves (inputs, secrets, outputs, defaults and the
explanatory comment at the top of a workflow), so they cannot drift from the code. Nothing is
written to disk: the pages exist only in the build, and in ``mkdocs serve``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files

REPOSITORY = "gold-standard-phantoms/github-actions"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"


def _latest_release() -> str:
    """Return the newest release tag reachable from HEAD, for the usage examples."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "vX.Y.Z"
    return result.stdout.strip() or "vX.Y.Z"


def _cell(value: Any) -> str:
    """Format a value for a Markdown table cell."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = str(value).lower()
    text = " ".join(str(value).split())
    # Descriptions hold placeholders such as <owner>, which Markdown would read as HTML tags.
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
    )


def _code(value: Any) -> str:
    """Format a default value as inline code, or a dash when there is none."""
    if value is None:
        return "–"
    if isinstance(value, bool):
        value = str(value).lower()
    text = str(value).replace("|", "\\|")
    return f"`{text}`" if text else '`""`'


def _header_comment(path: Path) -> str:
    """Return the comment block between a workflow's ``name:`` line and its ``on:`` key."""
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if line.startswith("#"):
            lines.append(line[1:].removeprefix(" "))
        elif line.strip() == "" and not lines:
            continue
        elif line.strip() == "":
            lines.append("")
        else:
            break
    return "\n".join(lines).strip()


def _table(rows: dict[str, Any] | None, columns: list[str]) -> str:
    """Render an inputs, secrets or outputs mapping as a Markdown table."""
    if not rows:
        return "None.\n"
    header = "| Name | " + " | ".join(columns) + " |\n"
    header += "|---" * (len(columns) + 1) + "|\n"
    body = ""
    for name, spec in rows.items():
        spec = spec or {}
        cells = []
        for column in columns:
            if column == "Required":
                cells.append("yes" if spec.get("required") else "no")
            elif column == "Default":
                cells.append(_code(spec.get("default")))
            elif column == "Type":
                cells.append(_cell(spec.get("type")))
            else:
                cells.append(_cell(spec.get("description")))
        body += f"| `{name}` | " + " | ".join(cells) + " |\n"
    return header + body


def _workflow_page(path: Path, release: str) -> tuple[str, str] | None:
    """Return the title and Markdown for a reusable workflow, or None if it is not reusable."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    triggers = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` as true
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        return None
    call = triggers["workflow_call"] or {}
    title = f"{path.name}"
    lines = [f"# `{path.name}`", "", f"**{_cell(data.get('name'))}**", ""]
    comment = _header_comment(path)
    if comment:
        lines += [
            "From the comment at the top of the workflow file:",
            "",
            "```text",
            comment,
            "```",
            "",
        ]
    lines += [
        "## Usage",
        "",
        "```yaml",
        "jobs:",
        "  <job>:",
        f"    uses: {REPOSITORY}/.github/workflows/{path.name}@{release}",
        "```",
        "",
        "## Inputs",
        "",
        _table(call.get("inputs"), ["Type", "Required", "Default", "Description"]),
        "## Secrets",
        "",
        _table(call.get("secrets"), ["Required", "Description"]),
        "## Outputs",
        "",
        _table(call.get("outputs"), ["Description"]),
        f"Source: [`.github/workflows/{path.name}`](https://github.com/{REPOSITORY}/blob/main/.github/workflows/{path.name})",
        "",
    ]
    return title, "\n".join(lines)


def _action_page(path: Path, release: str) -> tuple[str, str]:
    """Return the title and Markdown for a composite action."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = path.parent.name
    lines = [
        f"# `{name}`",
        "",
        f"**{_cell(data.get('name'))}**",
        "",
        _cell(data.get("description")),
        "",
        "## Usage",
        "",
        "```yaml",
        "steps:",
        f"  - uses: {REPOSITORY}/.github/actions/{name}@{release}",
        "```",
        "",
        "## Inputs",
        "",
        _table(data.get("inputs"), ["Required", "Default", "Description"]),
        "## Outputs",
        "",
        _table(data.get("outputs"), ["Description"]),
        f"Source: [`.github/actions/{name}/action.yml`](https://github.com/{REPOSITORY}/blob/main/.github/actions/{name}/action.yml)",
        "",
    ]
    return name, "\n".join(lines)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Add the generated reference pages and their index to the build."""
    release = _latest_release()
    workflows: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        page = _workflow_page(path, release)
        if page is None:
            continue
        uri = f"reference/workflows/{_slug(path.stem)}.md"
        files.append(File.generated(config, uri, content=page[1]))
        workflows.append((page[0], uri))

    actions: list[tuple[str, str]] = []
    for path in sorted(ACTIONS.glob("*/action.yml")):
        title, content = _action_page(path, release)
        uri = f"reference/actions/{_slug(path.parent.name)}.md"
        files.append(File.generated(config, uri, content=content))
        actions.append((title, uri))

    index = [
        "# Reference",
        "",
        "Generated from the YAML files at build time.",
        "",
        "## Reusable workflows",
        "",
    ]
    index += [
        f"- [`{title}`]({uri.removeprefix('reference/')})" for title, uri in workflows
    ]
    index += ["", "## Composite actions", ""]
    index += [
        f"- [`{title}`]({uri.removeprefix('reference/')})" for title, uri in actions
    ]
    files.append(
        File.generated(config, "reference/index.md", content="\n".join(index) + "\n")
    )
    return files
