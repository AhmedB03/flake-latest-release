"""Command-line interface: `check` and `update`."""

import argparse
import difflib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from .flake import FlakeInput, parse_flake, parse_version, rewrite_flake_text
from .github import GitHubError, NotFound, http_get_json, latest

OK, OUTDATED, AHEAD, NO_RELEASE, REV_PINNED, ERROR = (
    "ok", "outdated", "ahead", "no-release", "rev-pinned", "error")


@dataclass
class Status:
    input: FlakeInput
    status: str
    latest: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None  # "release" or "tag"
    error: Optional[str] = None

    @property
    def outdated(self):
        return self.status == OUTDATED

    def as_dict(self):
        i = self.input
        d = {"name": i.name, "owner": i.owner, "repo": i.repo, "line": i.line,
             "current": i.ref, "latest": self.latest, "outdated": self.outdated,
             "status": self.status, "source": self.source, "url": self.url}
        if self.error:
            d["error"] = self.error
        return d


class UsageError(Exception):
    pass


def run_command(argv, cwd=None):
    return subprocess.run(argv, check=True, cwd=cwd)


def flake_path(path):
    return os.path.join(path, "flake.nix") if os.path.isdir(path) else path


def read_text(path):
    # newline="" keeps CRLF files byte-identical on rewrite
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def select(inputs, names, all_inputs):
    """By default: inputs with a marker, plus inputs already pinned to a
    version tag. --input and --all also reach the rest."""
    if names:
        known = {i.name for i in inputs}
        missing = [n for n in names if n not in known]
        if missing:
            raise UsageError(
                "no github: input named " + ", ".join(missing)
                + (f" (found: {', '.join(sorted(known))})" if known else ""))
        return [i for i in inputs if i.name in names]
    if all_inputs:
        return inputs
    return [i for i in inputs if i.tracked or i.version_pinned]


def compare(current, latest_tag):
    """ok, outdated, or ahead (pinned to a higher version than the latest
    stable one, which `update` must not downgrade)."""
    if current == latest_tag:
        return OK
    cur, new = parse_version(current), parse_version(latest_tag)
    if cur and new and cur[0] == new[0] and new[1] <= cur[1]:
        return OK if new[1] == cur[1] else AHEAD
    return OUTDATED


def evaluate(inputs, fetch=http_get_json, token=None):
    out = []
    for inp in inputs:
        if inp.rev_pinned:
            out.append(Status(inp, REV_PINNED))
            continue
        try:
            rel = latest(inp.owner, inp.repo, inp.prefix, inp.ref, fetch=fetch, token=token)
        except NotFound:
            out.append(Status(inp, ERROR,
                              error=f"repository {inp.owner}/{inp.repo} not found"))
            continue
        except GitHubError as e:
            out.append(Status(inp, ERROR, error=str(e)))
            continue
        if rel is None:
            out.append(Status(inp, NO_RELEASE))
        else:
            out.append(Status(inp, compare(inp.ref, rel.tag), rel.tag, rel.url, rel.source))
    return out


def print_table(statuses):
    rows = [("INPUT", "CURRENT", "LATEST", "STATUS", "")]
    for s in statuses:
        note = s.error or (s.url if s.outdated else "") or ""
        rows.append((s.input.name, s.input.ref or "-", s.latest or "-", s.status, note))
    widths = [max(len(r[c]) for r in rows) for c in range(4)]
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in range(4)) + ("  " + r[4] if r[4] else ""))


def _load(args, fetch):
    path = flake_path(args.flake)
    text = read_text(path)
    inputs = parse_flake(text)
    chosen = select(inputs, args.input, args.all)
    if not chosen:
        print(f"{path}: no inputs to check. Inputs pinned to a version tag are "
              "checked automatically; for others put `# track: latest-release` "
              "after the url, or pass --input NAME or --all.", file=sys.stderr)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    return path, text, evaluate(chosen, fetch=fetch, token=token)


def cmd_check(args, fetch):
    _, _, statuses = _load(args, fetch)
    if args.json:
        print(json.dumps([s.as_dict() for s in statuses], indent=2))
    elif statuses:
        print_table(statuses)
    if any(s.status == ERROR for s in statuses):
        return 2
    return 1 if any(s.outdated for s in statuses) else 0


def cmd_update(args, fetch, run):
    path, text, statuses = _load(args, fetch)
    failed = False
    changes = []
    for s in statuses:
        name = s.input.name
        if s.status == REV_PINNED:
            print(f"warning: {name} is pinned to a rev, skipping", file=sys.stderr)
        elif s.status == NO_RELEASE:
            print(f"warning: {name} ({s.input.owner}/{s.input.repo}) has no "
                  "matching release, skipping", file=sys.stderr)
        elif s.status == ERROR:
            print(f"error: {name}: {s.error}", file=sys.stderr)
            failed = True
        elif s.outdated:
            changes.append((s.input, s.latest))
    new_text = rewrite_flake_text(text, changes)
    if args.dry_run:
        sys.stdout.writelines(difflib.unified_diff(
            text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=path, tofile=path))
        return 2 if failed else 0
    if changes:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        for inp, tag in changes:
            print(f"{inp.name}: {inp.ref or '(no ref)'} -> {tag}")
        if args.lock:
            cwd = os.path.dirname(os.path.abspath(path))
            for inp, _ in changes:
                try:
                    run(["nix", "flake", "update", inp.name], cwd=cwd)
                except FileNotFoundError:
                    print("error: --lock needs `nix` on PATH; flake.nix was "
                          "updated, flake.lock was not", file=sys.stderr)
                    return 2
                except subprocess.CalledProcessError as e:
                    print(f"error: `nix flake update {inp.name}` exited with "
                          f"{e.returncode}", file=sys.stderr)
                    return 2
    return 2 if failed else 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="flake-latest-release",
        description="Check or pin github: flake inputs against each repo's "
                    "latest GitHub release.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("flake", nargs="?", default=".",
                        help="flake.nix or its directory (default: .)")
        sp.add_argument("--input", action="append", metavar="NAME",
                        help="only this input, whatever it points at (repeatable)")
        sp.add_argument("--all", action="store_true",
                        help="every github: input, including ones following a branch")

    pc = sub.add_parser("check", help="report inputs that are behind their latest "
                                      "release; exit 1 if any are")
    common(pc)
    pc.add_argument("--json", action="store_true")

    pu = sub.add_parser("update", help="rewrite refs in flake.nix to the latest release")
    common(pu)
    pu.add_argument("--lock", action="store_true",
                    help="then run `nix flake update NAME` for each changed input")
    pu.add_argument("--dry-run", action="store_true", help="print a diff, write nothing")
    return p


def main(argv=None, fetch=http_get_json, run=run_command):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "check":
            return cmd_check(args, fetch)
        return cmd_update(args, fetch, run)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
