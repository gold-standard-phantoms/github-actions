#!/usr/bin/env python3
# COPY of docs-server client/gsp_docs.py — do not edit here; change it there and run
# client/sync-to-github-actions.sh.
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""gsp-docs: publish a built documentation site to the GSP documentation server.

Standard library only, so it runs on a laptop or a GitHub runner with nothing installed:

    export GSP_DOCS_KEY=gspdocs_pub_...          # or --key-file ~/.config/gsp-docs/qmri.key
    python3 gsp_docs.py whoami
    python3 gsp_docs.py publish site --project qmri --version 0.2.0 --alias latest

The key is read from the environment or a file, never from the command line, where other
users and shell history could see it (R-CI-3). In GitHub Actions the version, commit, ref and
run URL default from the workflow's environment (R-CI-2): a tag ``v1.2.0`` publishes ``1.2.0``
and moves ``latest``; a branch publishes ``dev``.

The canonical copy lives in the docs-server repository (``client/gsp_docs.py``) and is tested
against the server there. ``gold-standard-phantoms/github-actions`` carries a copy for the
``publish-docs`` action; refresh it with ``client/sync-to-github-actions.sh``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

__version__ = "0.1.0"

DEFAULT_SERVER = "https://docs.gsplabs.com"
KEY_ENV = "GSP_DOCS_KEY"
SERVER_ENV = "GSP_DOCS_URL"
_KEY_RE = re.compile(r"^gspdocs_(pub|read)_[A-Za-z0-9_-]{20,}$")
_RELEASE_RE = re.compile(r"^\d+(\.\d+)*$")
_RETRY_STATUSES = frozenset({502, 503, 504})
_RETRY_DELAYS = (2.0, 5.0)
_EXPIRY_WARNING_DAYS = 30


class UsageError(Exception):
    """A problem with the command line or configuration (exit status 2)."""


class PublishFailed(Exception):
    """The server refused the request or could not be reached (exit status 1)."""


@dataclass
class Target:
    """What to publish, after defaults from the environment are applied."""

    version: str
    aliases: list[str]
    commit: str = ""
    ref: str = ""
    run_url: str = ""
    message: str = ""


@dataclass
class Packed:
    """A packed site in a temporary file."""

    file: IO[bytes]
    size: int
    files: int
    unpacked_bytes: int
    skipped: list[str] = field(default_factory=list)


# --- configuration ---------------------------------------------------------------------------


def read_key(env: Mapping[str, str], key_file: str | None) -> str:
    """The API key from ``GSP_DOCS_KEY`` or a key file.

    Args:
        env: Environment variables.
        key_file: Path given with ``--key-file``, if any.

    Returns:
        The key.

    Raises:
        UsageError: If there is no key, the file is readable by others, or the key is malformed.
    """
    if key_file:
        path = Path(key_file).expanduser()
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise UsageError(f"Cannot read the key file {path}: {exc.strerror}.") from exc
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise UsageError(f"{path} is readable by other users; run: chmod 600 {path}")
        key = path.read_text(encoding="utf-8").strip()
    else:
        key = env.get(KEY_ENV, "").strip()
    if not key:
        raise UsageError(f"No API key: set {KEY_ENV} or pass --key-file.")
    if not _KEY_RE.match(key):
        raise UsageError(
            "That does not look like a docs-server key (gspdocs_pub_… or gspdocs_read_…)."
        )
    return key


def check_server(url: str) -> str:
    """Normalise the server URL and refuse plain HTTP to anything but this machine.

    Raises:
        UsageError: If the URL is not https (or http to a loopback name).
    """
    parsed = urllib.parse.urlsplit(url.rstrip("/"))
    host = parsed.hostname or ""
    loopback = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    if parsed.scheme == "https" or (parsed.scheme == "http" and loopback):
        if parsed.path or parsed.query:
            raise UsageError(
                "The server URL is just scheme and host, e.g. https://docs.gsplabs.com"
            )
        return f"{parsed.scheme}://{parsed.netloc}"
    raise UsageError(f"Refusing to send a key to {url}: use https (http only to localhost).")


def _git(*args: str, cwd: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed program, arguments are ours
            ["git", *args],  # noqa: S607 - git from PATH, as a developer would run it
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def resolve_target(args: argparse.Namespace, env: Mapping[str, str], cwd: Path) -> Target:
    """Work out version, aliases and build metadata from the options and the environment.

    In GitHub Actions (``GITHUB_ACTIONS=true``), with no ``--version``: a tag publishes the
    tag name without a leading ``v`` and, for a plain release number, moves ``latest``; a branch
    publishes ``dev``. Outside Actions, ``--version`` is required and the commit comes from git.

    Raises:
        UsageError: If no version can be determined.
    """
    in_actions = env.get("GITHUB_ACTIONS") == "true"
    version: str = args.version or ""
    aliases: list[str] = list(args.alias or [])
    if not version:
        if not in_actions:
            raise UsageError("--version is required outside GitHub Actions.")
        ref_type, ref_name = env.get("GITHUB_REF_TYPE", ""), env.get("GITHUB_REF_NAME", "")
        if ref_type == "tag" and ref_name:
            version = ref_name[1:] if re.match(r"^v\d", ref_name) else ref_name
            if not aliases and not args.no_aliases and _RELEASE_RE.match(version):
                aliases = ["latest"]
        elif ref_type == "branch":
            version = "dev"
        else:
            raise UsageError("Cannot tell the version from this event; pass --version.")
    if args.no_aliases:
        aliases = []
    if in_actions:
        commit = args.commit or env.get("GITHUB_SHA", "")
        ref = args.ref or env.get("GITHUB_REF", "")
        run_url = ""
        if env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
            server = env.get("GITHUB_SERVER_URL", "https://github.com")
            run_url = f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
        message = (
            args.message
            or f"{env.get('GITHUB_REPOSITORY', '')} {env.get('GITHUB_WORKFLOW', '')}".strip()
        )
    else:
        commit = args.commit or _git("rev-parse", "HEAD", cwd=cwd)
        if commit and not args.commit and _git("status", "--porcelain", cwd=cwd):
            commit = f"{commit[:40]}-dirty"
        ref = args.ref or _git("symbolic-ref", "-q", "HEAD", cwd=cwd)
        run_url = ""
        message = args.message or f"gsp-docs from {getpass.getuser()}@{socket.gethostname()}"
    return Target(version, aliases, commit, ref, run_url, message)


# --- packing ---------------------------------------------------------------------------------


def pack_site(site: Path) -> Packed:
    """Pack a built site into a ``.tar.gz`` in a temporary file.

    Only regular files are packed, with names relative to the site root. The server applies
    the same rules (R-PUB-3); checking here gives a clearer message before uploading.

    Raises:
        UsageError: If the directory is missing, has no ``index.html``, or holds symlinks.
    """
    if not site.is_dir():
        raise UsageError(f"{site} is not a directory (build the site first).")
    if not (site / "index.html").is_file():
        raise UsageError(f"{site} has no index.html at its root.")
    links = [p for p in site.rglob("*") if p.is_symlink()]
    if links:
        names = ", ".join(str(p.relative_to(site)) for p in links[:5])
        raise UsageError(f"Symbolic links cannot be published: {names}")
    paths = sorted(p for p in site.rglob("*") if p.is_file())
    spool: IO[bytes] = tempfile.TemporaryFile()  # noqa: SIM115 - returned to the caller
    unpacked = 0
    with tarfile.open(fileobj=spool, mode="w:gz") as tar:
        for path in paths:
            relative = path.relative_to(site).as_posix()
            info = tar.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            unpacked += info.size
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    size = spool.tell()
    spool.seek(0)
    return Packed(spool, size, len(paths), unpacked)


# --- HTTP ------------------------------------------------------------------------------------


@dataclass
class Client:
    """A tiny client for the docs-server API."""

    server: str
    key: str
    timeout: float = 300.0
    sleep: Any = time.sleep

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: IO[bytes] | None = None,
        size: int = 0,
        content_type: str = "",
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": f"gsp-docs/{__version__}",
        }
        if body is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(size)
        attempts = len(_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            if body is not None:
                body.seek(0)
            request = urllib.request.Request(  # noqa: S310 - scheme checked in check_server
                self.server + path, data=body, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    payload: dict[str, Any] = json.loads(response.read() or b"{}")
                    return payload
            except urllib.error.HTTPError as exc:
                detail = _detail(exc)
                if exc.code in _RETRY_STATUSES and attempt < attempts - 1:
                    _note(f"Server answered {exc.code}; retrying.")
                    self.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise PublishFailed(_explain(exc.code, detail)) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt < attempts - 1:
                    _note(f"Could not reach {self.server} ({_reason(exc)}); retrying.")
                    self.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise PublishFailed(f"Could not reach {self.server}: {_reason(exc)}") from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def whoami(self) -> dict[str, Any]:
        """``GET /api/v1/whoami``: the key's project, scope and expiry."""
        return self._request("GET", "/api/v1/whoami")

    def publish(
        self, project: str, target: Target, packed: Packed, *, dry_run: bool, default: bool
    ) -> dict[str, Any]:
        """``PUT /api/v1/projects/<project>/versions/<version>`` with the packed site."""
        query: list[tuple[str, str]] = [("aliases", alias) for alias in target.aliases]
        for name, value in (
            ("commit", target.commit),
            ("ref", target.ref),
            ("run_url", target.run_url),
            ("message", target.message),
        ):
            if value:
                query.append((name, value))
        if default:
            query.append(("default", "true"))
        if dry_run:
            query.append(("dry_run", "true"))
        path = (
            f"/api/v1/projects/{urllib.parse.quote(project, safe='')}"
            f"/versions/{urllib.parse.quote(target.version, safe='')}"
        )
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request(
            "PUT", path, body=packed.file, size=packed.size, content_type="application/gzip"
        )


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return ""
    detail = body.get("detail", "") if isinstance(body, dict) else ""
    return detail if isinstance(detail, str) else json.dumps(detail)


def _explain(status: int, detail: str) -> str:
    hints = {
        401: "the key is wrong, expired or revoked",
        404: "the key does not belong to this project, or the project does not exist",
        413: "the archive is larger than the project's upload limit",
    }
    text = f"Server refused the request ({status})"
    if detail:
        text += f": {detail}"
    if status in hints:
        text += f" — {hints[status]}"
    return text


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    return str(reason)


def _note(message: str) -> None:
    print(message, file=sys.stderr)


# --- GitHub Actions integration --------------------------------------------------------------


def _append(env: Mapping[str, str], variable: str, text: str) -> None:
    path = env.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _expiry_days(expires_at: str | None) -> int | None:
    if not expires_at:
        return None
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).days


def _warn_expiry(identity: Mapping[str, Any], env: Mapping[str, str]) -> None:
    days = _expiry_days(identity.get("expires_at"))
    if days is not None and days < _EXPIRY_WARNING_DAYS:
        text = (
            f"The docs-server key '{identity.get('key_name')}' expires in {days} day(s); "
            "ask an admin for a new one."
        )
        if env.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::{text}")
        else:
            _note(f"Warning: {text}")


# --- commands --------------------------------------------------------------------------------


def cmd_whoami(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    client = Client(
        check_server(args.server or env.get(SERVER_ENV) or DEFAULT_SERVER),
        read_key(env, args.key_file),
    )
    identity = client.whoami()
    if args.json:
        print(json.dumps(identity, indent=2))
    else:
        expires = identity.get("expires_at") or "never"
        print(
            f"Key '{identity['key_name']}': {identity['scope']} for project "
            f"{identity['project']}, expires {expires}"
        )
    _warn_expiry(identity, env)
    return 0


def cmd_publish(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    server = check_server(args.server or env.get(SERVER_ENV) or DEFAULT_SERVER)
    key = read_key(env, args.key_file)
    if not key.startswith("gspdocs_pub_"):
        raise UsageError("This is a read key; publishing needs a publish key (gspdocs_pub_…).")
    target = resolve_target(args, env, Path.cwd())
    client = Client(server, key)
    identity = client.whoami()
    if identity.get("project") != args.project:
        raise UsageError(
            f"This key belongs to project '{identity.get('project')}', not '{args.project}'."
        )
    _warn_expiry(identity, env)
    packed = pack_site(Path(args.site))
    try:
        _note(
            f"Uploading {packed.files} files ({packed.unpacked_bytes:,} bytes, "
            f"{packed.size:,} packed) as {args.project} {target.version}"
            + (f" with {', '.join(target.aliases)}" if target.aliases else "")
            + (" (dry run)" if args.dry_run else "")
        )
        result = client.publish(
            args.project, target, packed, dry_run=args.dry_run, default=args.default
        )
    finally:
        packed.file.close()
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        print(
            f"Dry run: {result['files']} files; {result['added']} added, {result['changed']} "
            f"changed, {result['removed']} removed, {result['unchanged']} unchanged."
        )
    else:
        print(
            f"Published {result['project']} {result['version']} "
            f"(deployment {result['id']}): {result['url']}"
        )
    if not args.dry_run:
        _append(
            env,
            "GITHUB_OUTPUT",
            f"url={result['url']}\ndeployment_id={result['id']}\nversion={result['version']}\n",
        )
        aliases = ", ".join(result.get("aliases") or []) or "none"
        _append(
            env,
            "GITHUB_STEP_SUMMARY",
            "### Documentation published\n\n"
            f"[{result['project']} {result['version']}]({result['url']}) — "
            f"deployment {result['id']}, {result['files']} files, aliases: {aliases}\n",
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsp-docs",
        description="Publish a built site to the GSP documentation server.",
        epilog=f"The key comes from ${KEY_ENV} or --key-file, never the command line.",
    )
    parser.add_argument("--version", action="version", version=f"gsp-docs {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", help=f"Server URL (default ${SERVER_ENV} or {DEFAULT_SERVER}).")
    common.add_argument(
        "--key-file", help=f"File holding the key (instead of ${KEY_ENV}); must be chmod 600."
    )
    common.add_argument("--json", action="store_true", help="Print the server's JSON answer.")
    commands = parser.add_subparsers(dest="command", required=True)

    whoami = commands.add_parser(
        "whoami", parents=[common], help="Show which project and scope a key has."
    )
    whoami.set_defaults(handler=cmd_whoami)

    publish = commands.add_parser(
        "publish", parents=[common], help="Upload a built site as a version."
    )
    publish.add_argument("site", help="The built site directory (e.g. site/ from mkdocs build).")
    publish.add_argument("--project", required=True, help="Project slug, e.g. qmri.")
    # dest "version" would clash with --version on the main parser.
    publish.add_argument(
        "--version", dest="version", help="Version name (in GitHub Actions: from the tag, or dev)."
    )
    publish.add_argument(
        "--alias", action="append", help="Alias to point at this version (repeatable), e.g. latest."
    )
    publish.add_argument(
        "--no-aliases",
        action="store_true",
        help="Do not move any alias, not even the default latest for a tag.",
    )
    publish.add_argument(
        "--default", action="store_true", help="Make this version the project's default."
    )
    publish.add_argument(
        "--dry-run", action="store_true", help="Check the archive and report what would change."
    )
    publish.add_argument("--commit", help="Commit SHA (default: from git or GITHUB_SHA).")
    publish.add_argument("--ref", help="Git ref (default: from git or GITHUB_REF).")
    publish.add_argument("--message", help="A note stored with the deployment.")
    publish.set_defaults(handler=cmd_publish)
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the command line; return the exit status (0 ok, 1 refused or unreachable, 2 usage)."""
    environment = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    try:
        status: int = args.handler(args, environment)
    except UsageError as exc:
        _note(f"gsp-docs: {exc}")
        return 2
    except PublishFailed as exc:
        _note(f"gsp-docs: {exc}")
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
