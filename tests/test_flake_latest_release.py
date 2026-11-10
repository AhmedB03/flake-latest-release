import json

import pytest

from flake_latest_release import (
    latest, latest_release, main, parse_flake, parse_version, pick_release,
    pick_tag, rewrite_flake_text,
)
from flake_latest_release.github import GitHubError, NotFound

FLAKE = '''{
  description = "example { with braces } and inputs.fake.url = \\"github:x/y\\";";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    # track: latest-release
    nix.url = "github:NixOS/nix/2.18.0";
    home-manager = {
      url = "github:nix-community/home-manager/release-24.05"; # track: latest-release prefix=release-
      inputs.nixpkgs.follows = "nixpkgs";
    };
    /* inputs.commented.url = "github:no/pe"; */
    pinned.url = "github:o/pinned?rev=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; # track: latest-release
    local.url = "path:./local";
  };
  inputs.tool.url = "github:o/tool?ref=refs/tags/v1&dir=sub"; # track: latest-release
  inputs."quoted-name" = { url = "github:o/q"; flake = false; };

  outputs = { self, nixpkgs, ... }@inputs: let
    x = { inputs.inner.url = "github:not/an-input"; };
  in {
    packages = { url = "github:also/not"; };
  };
}
'''


def fake_fetch(table, seen=None):
    def fetch(url, token=None):
        if seen is not None:
            seen.append(url)
        for key, val in table.items():
            if url.endswith(key):
                if isinstance(val, Exception):
                    raise val
                return val
        if "/tags?" in url and not url.startswith("https://api.github.com/repos/gone/"):
            return []  # an existing repository without tags
        raise NotFound(url)
    return fetch


def write(tmp_path, text=FLAKE):
    p = tmp_path / "flake.nix"
    p.write_bytes(text.encode("utf-8"))
    return str(p)


def by_name(text=FLAKE):
    return {i.name: i for i in parse_flake(text)}


# ------------------------------------------------------------------ parsing

def test_finds_only_real_inputs_in_every_layout():
    assert [i.name for i in parse_flake(FLAKE)] == [
        "nixpkgs", "nix", "home-manager", "pinned", "tool", "quoted-name"]


def test_input_fields():
    inputs = by_name()
    n = inputs["nixpkgs"]
    assert (n.owner, n.repo, n.ref, n.tracked, n.line) == (
        "NixOS", "nixpkgs", "nixos-24.05", False, 5)
    assert inputs["quoted-name"].ref is None
    assert inputs["tool"].ref == "v1"  # refs/tags/ stripped for comparison
    assert inputs["pinned"].rev_pinned
    assert parse_flake('{ inputs.a.url = "github:o/r/' + "b" * 40 + '"; }')[0].rev_pinned


def test_markers_same_line_and_line_above():
    inputs = by_name()
    assert inputs["nix"].tracked and inputs["nix"].prefix is None
    assert inputs["home-manager"].tracked and inputs["home-manager"].prefix == "release-"
    assert not inputs["nixpkgs"].tracked
    # a marker on the previous input's line does not leak to the next line
    two = '{ inputs = {\n a.url = "github:o/a"; # track: latest-release\n b.url = "github:o/b";\n}; }'
    assert [i.tracked for i in parse_flake(two)] == [True, False]


def test_first_input_in_inputs_block_keeps_its_own_name():
    text = '{\n  inputs = {\n    nixpkgs.url = "github:NixOS/nixpkgs";\n  };\n}\n'
    assert parse_flake(text)[0].name == "nixpkgs"


def test_strings_with_interpolation_and_indented_strings_do_not_confuse():
    text = '''{
  description = ''
    a } stray brace and ''${not} "quotes"
  '';
  inputs.a.url = "github:o/a/v1";
  outputs = _: { x = "${toString { a = "}"; }}"; };
}'''
    assert [(i.name, i.ref) for i in parse_flake(text)] == [("a", "v1")]


# ---------------------------------------------------------------- rewriting

def test_rewrite_forms():
    def rewritten(url, tag):
        text = '{ inputs.x.url = "%s"; }' % url
        return rewrite_flake_text(text, [(parse_flake(text)[0], tag)])

    assert '"github:o/r/v2"' in rewritten("github:o/r/v1", "v2")
    assert '"github:o/r/v3.0.0"' in rewritten("github:o/r", "v3.0.0")
    assert '"github:o/r/v2?dir=sub"' in rewritten("github:o/r/v1?dir=sub", "v2")
    assert '"github:o/r?ref=v2&submodules=1"' in rewritten("github:o/r?ref=v1&submodules=1", "v2")
    assert '"github:o/r?ref=refs/tags/v2"' in rewritten("github:o/r?ref=refs/tags/v1", "v2")
    # a tag with a slash cannot be a path segment
    assert '"github:o/r?ref=rel/2&dir=d"' in rewritten("github:o/r/v1?dir=d", "rel/2")


def test_rewrite_changes_nothing_else():
    inputs = by_name()
    out = rewrite_flake_text(FLAKE, [(inputs["nix"], "2.24.5"), (inputs["tool"], "v2")])
    assert out == (FLAKE.replace("github:NixOS/nix/2.18.0", "github:NixOS/nix/2.24.5")
                   .replace("ref=refs/tags/v1&dir=sub", "ref=refs/tags/v2&dir=sub"))


# --------------------------------------------------------------- resolution

def test_latest_release_uses_github_latest_endpoint():
    seen = []
    fetch = fake_fetch({"/repos/NixOS/nix/releases/latest":
                        {"tag_name": "2.24.5", "html_url": "https://x/2.24.5"}}, seen)
    rel = latest_release("NixOS", "nix", fetch=fetch)
    assert (rel.tag, rel.url) == ("2.24.5", "https://x/2.24.5")
    assert seen == ["https://api.github.com/repos/NixOS/nix/releases/latest"]
    assert latest_release("o", "none", fetch=fake_fetch({})) is None


def test_prefix_skips_drafts_and_prereleases():
    releases = [
        {"tag_name": "v3.0.0", "published_at": "2025-03-01"},
        {"tag_name": "v2.9.1", "prerelease": True, "published_at": "2025-02-01"},
        {"tag_name": "v2.8.5", "published_at": "2025-01-20"},
        {"tag_name": "v2.9.0", "published_at": "2025-01-10"},
        {"tag_name": "v2.10.0", "draft": True, "published_at": None},
    ]
    assert pick_release(releases, "v2.").tag == "v2.8.5"  # most recently published
    assert pick_release(releases, "v9.") is None


def test_prefix_pages_until_a_match():
    page1 = [{"tag_name": f"v3.{i}", "published_at": "2025"} for i in range(100)]
    page2 = [{"tag_name": "v2.9.0", "published_at": "2024"}]
    seen = []
    fetch = fake_fetch({"per_page=100&page=1": page1, "per_page=100&page=2": page2}, seen)
    assert latest_release("o", "r", "v2.", fetch=fetch).tag == "v2.9.0"
    assert len(seen) == 2
    # stops at a short page without a match
    assert latest_release("o", "r", "v1.", fetch=fetch) is None


def test_parse_version():
    assert parse_version("v1.2.3") == ("v", (1, 2, 3))
    assert parse_version("2.24.0") == ("", (2, 24, 0))
    assert parse_version("release-24.05") == ("release-", (24, 5))
    for not_stable in ("v2.0.0-rc1", "2.4pre", "main", "mnt-reform2-v1", None):
        assert parse_version(not_stable) is None


def test_pick_tag_is_highest_stable_in_the_same_family():
    names = ["2.9.1", "2.10.0", "2.11.0-rc1", "2.11.0pre", "v9.0.0", "latest", "cli-3.0"]
    assert pick_tag(names, current="2.4.0") == "2.10.0"  # numeric, not string, order
    assert pick_tag(names, current="v1") == "v9.0.0"
    assert pick_tag(names, current="main") == "v9.0.0"  # plain and v- tags compete
    assert pick_tag(names, prefix="2.9.") == "2.9.1"
    assert pick_tag(names, prefix="cli-") == "cli-3.0"
    assert pick_tag(["nightly"], current=None) is None


def test_falls_back_to_tags_when_repo_publishes_no_releases():
    seen = []
    fetch = fake_fetch({"/repos/NixOS/nix/tags?per_page=100&page=1":
                        [{"name": "2.31.0"}, {"name": "2.32.0-rc1"}, {"name": "2.30.2"}]}, seen)
    rel = latest("NixOS", "nix", current="2.24.0", fetch=fetch)
    assert (rel.tag, rel.source) == ("2.31.0", "tag")
    assert rel.url == "https://github.com/NixOS/nix/releases/tag/2.31.0"
    assert [u.rsplit("/", 1)[1] for u in seen] == ["latest", "tags?per_page=100&page=1"]
    # a published release wins and the tags are never fetched
    seen.clear()
    fetch = fake_fetch({"/repos/o/r/releases/latest": {"tag_name": "v2"}}, seen)
    assert latest("o", "r", fetch=fetch).source == "release" and len(seen) == 1


# ---------------------------------------------------------------------- cli

TABLE = {
    "/repos/NixOS/nix/releases/latest": {"tag_name": "2.24.5", "html_url": "https://gh/nix/2.24.5"},
    "/repos/nix-community/home-manager/releases?per_page=100&page=1":
        [{"tag_name": "release-24.05", "published_at": "2024-06-01"}],
    "/repos/o/tool/releases/latest": {"tag_name": "v2"},
    "/repos/o/q/releases/latest": {"tag_name": "q1"},
}


def test_check_covers_marked_and_version_pinned_inputs_and_exits_1(tmp_path, capsys):
    p = write(tmp_path)
    code = main(["check", p, "--json"], fetch=fake_fetch(TABLE))
    data = {d["name"]: d for d in json.loads(capsys.readouterr().out)}
    assert code == 1
    # unmarked and left alone: nixpkgs follows a release branch, quoted-name
    # the default branch
    assert set(data) == {"nix", "home-manager", "pinned", "tool"}
    assert data["nix"] == {
        "name": "nix", "owner": "NixOS", "repo": "nix", "line": 7, "current": "2.18.0",
        "latest": "2.24.5", "outdated": True, "status": "outdated",
        "source": "release", "url": "https://gh/nix/2.24.5"}
    assert data["home-manager"]["status"] == "ok"
    assert data["pinned"]["status"] == "rev-pinned"


def test_check_exit_0_when_current_and_table_output(tmp_path, capsys):
    p = write(tmp_path, '{ inputs.nix.url = "github:NixOS/nix/2.24.5"; # track: latest-release\n}')
    assert main(["check", p], fetch=fake_fetch(TABLE)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["INPUT", "CURRENT", "LATEST", "STATUS"]
    assert out[1].split() == ["nix", "2.24.5", "2.24.5", "ok"]


def test_check_accepts_directory_and_input_reaches_unmarked(tmp_path, capsys):
    write(tmp_path)
    code = main(["check", str(tmp_path), "--json", "--input", "quoted-name"],
                fetch=fake_fetch(TABLE))
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [(d["name"], d["current"], d["latest"]) for d in data] == [("quoted-name", None, "q1")]


def test_all_reaches_unmarked_inputs(tmp_path, capsys):
    p = write(tmp_path)
    main(["check", p, "--json", "--all"], fetch=fake_fetch(TABLE))
    data = {d["name"]: d["status"] for d in json.loads(capsys.readouterr().out)}
    assert data["nixpkgs"] == "no-release" and data["quoted-name"] == "outdated"


def test_version_pinned_input_needs_no_marker(tmp_path, capsys):
    p = write(tmp_path, '{ inputs.nix.url = "github:NixOS/nix/2.24.0"; }')
    fetch = fake_fetch({"/repos/NixOS/nix/tags?per_page=100&page=1": [{"name": "2.31.0"}]})
    assert main(["check", p, "--json"], fetch=fetch) == 1
    d = json.loads(capsys.readouterr().out)[0]
    assert (d["latest"], d["source"], d["status"]) == ("2.31.0", "tag", "outdated")
    assert main(["update", p], fetch=fetch) == 0
    assert open(p, encoding="utf-8").read() == '{ inputs.nix.url = "github:NixOS/nix/2.31.0"; }'


def test_release_branches_are_not_treated_as_version_pins():
    refs = {"2.24.0": True, "v0.41": True, "nixos-24.05": False, "release-24.05": False,
            "nixos-unstable": False, "main": False, "v2.0.0-rc1": False}
    for ref, expected in refs.items():
        inp = parse_flake('{ inputs.a.url = "github:o/r/%s"; }' % ref)[0]
        assert inp.version_pinned is expected, ref


def test_never_downgrades_a_pin_that_is_ahead(tmp_path, capsys):
    text = '{ inputs.t.url = "github:o/tool/v3"; }'
    p = write(tmp_path, text)
    assert main(["check", p, "--json"], fetch=fake_fetch(TABLE)) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "ahead"
    assert main(["update", p], fetch=fake_fetch(TABLE)) == 0
    assert open(p, encoding="utf-8").read() == text
    # v2.0 and v2 are the same version
    p = write(tmp_path, '{ inputs.t.url = "github:o/tool/v2.0"; }')
    assert main(["check", p], fetch=fake_fetch(TABLE)) == 0


def test_missing_repository_is_an_error(tmp_path, capsys):
    p = write(tmp_path, '{ inputs.x.url = "github:gone/repo/v1"; }')
    assert main(["check", p], fetch=fake_fetch({})) == 2
    assert "repository gone/repo not found" in capsys.readouterr().out


def test_unknown_input_name_is_an_error(tmp_path, capsys):
    p = write(tmp_path)
    assert main(["check", p, "--input", "nope"], fetch=fake_fetch(TABLE)) == 2
    assert "no github: input named nope" in capsys.readouterr().err


def test_api_failure_is_reported_and_exits_2(tmp_path, capsys):
    p = write(tmp_path)
    table = dict(TABLE)
    table["/repos/NixOS/nix/releases/latest"] = GitHubError("GitHub API rate limit reached")
    assert main(["check", p], fetch=fake_fetch(table)) == 2
    assert "rate limit" in capsys.readouterr().out
    before = open(p, encoding="utf-8", newline="").read()
    assert main(["update", p], fetch=fake_fetch(table)) == 2
    after = open(p, encoding="utf-8", newline="").read()
    assert "github:NixOS/nix/2.18.0" in after and after != before  # others still updated


def test_update_rewrites_marked_inputs_only(tmp_path, capsys):
    p = write(tmp_path)
    assert main(["update", p], fetch=fake_fetch(TABLE)) == 0
    out = open(p, encoding="utf-8", newline="").read()
    assert out == (FLAKE.replace("github:NixOS/nix/2.18.0", "github:NixOS/nix/2.24.5")
                   .replace("ref=refs/tags/v1&dir=sub", "ref=refs/tags/v2&dir=sub"))
    cap = capsys.readouterr()
    assert "nix: 2.18.0 -> 2.24.5" in cap.out
    assert "pinned is pinned to a rev" in cap.err


def test_update_preserves_crlf(tmp_path):
    text = '{\r\n  inputs.nix.url = "github:NixOS/nix/2.18.0"; # track: latest-release\r\n}\r\n'
    p = write(tmp_path, text)
    main(["update", p], fetch=fake_fetch(TABLE))
    assert open(p, "rb").read() == text.replace("2.18.0", "2.24.5").encode()


def test_dry_run_prints_diff_and_writes_nothing(tmp_path, capsys):
    p = write(tmp_path)
    calls = []
    code = main(["update", p, "--dry-run", "--lock"], fetch=fake_fetch(TABLE),
                run=lambda argv, cwd=None: calls.append(argv))
    out = capsys.readouterr().out
    assert code == 0 and calls == []
    assert open(p, encoding="utf-8", newline="").read() == FLAKE
    assert '-    nix.url = "github:NixOS/nix/2.18.0";' in out
    assert '+    nix.url = "github:NixOS/nix/2.24.5";' in out


def test_lock_runs_nix_once_per_changed_input_in_flake_dir(tmp_path):
    p = write(tmp_path)
    calls = []
    run = lambda argv, cwd=None: calls.append((argv, cwd))
    assert main(["update", p, "--lock"], fetch=fake_fetch(TABLE), run=run) == 0
    assert calls == [(["nix", "flake", "update", "nix"], str(tmp_path)),
                     (["nix", "flake", "update", "tool"], str(tmp_path))]
    calls.clear()
    assert main(["update", p, "--lock"], fetch=fake_fetch(TABLE), run=run) == 0
    assert calls == []  # nothing changed the second time


def test_lock_without_nix_says_so(tmp_path, capsys):
    p = write(tmp_path)

    def run(argv, cwd=None):
        raise FileNotFoundError("nix")
    assert main(["update", p, "--lock"], fetch=fake_fetch(TABLE), run=run) == 2
    assert "needs `nix` on PATH" in capsys.readouterr().err


def test_no_tracked_inputs_hint(tmp_path, capsys):
    p = write(tmp_path, '{ inputs.a.url = "github:o/a"; }')
    assert main(["check", p], fetch=fake_fetch({})) == 0
    assert "no inputs to check" in capsys.readouterr().err
