# flake-latest-release

Tells you when a `github:` input in your `flake.nix` has a newer **stable release**, and can
bump the pin for you.

Nix flake refs are static: `github:owner/repo/v1.2.0` stays at `v1.2.0` until you edit it, and
there is no way to say "the latest release" ([NixOS/nix#11946](https://github.com/NixOS/nix/issues/11946)).
So you either follow the default branch and take whatever is there, or pin a tag and find out
about new releases by accident. This is a small wrapper for the second group.

```console
$ flake-latest-release check
INPUT      CURRENT  LATEST   STATUS
nix        2.24.0   2.35.6   outdated  https://github.com/NixOS/nix/releases/tag/2.35.6
helix      24.07    25.07.1  outdated  https://github.com/helix-editor/helix/releases/tag/25.07.1
lan-mouse  v0.11.0  v0.11.0  ok
$ echo $?
1

$ flake-latest-release update --lock
nix: 2.24.0 -> 2.35.6
helix: 24.07 -> 25.07.1
```

## Install

```sh
nix run github:AhmedB03/flake-latest-release -- check
```

Or without Nix: it is pure Python (3.9+, standard library only), so `pip install .` from a
clone, or `PYTHONPATH=<clone> python3 -m flake_latest_release check`.

## Which inputs it looks at

By default, with nothing to configure:

* inputs pinned to a version tag, such as `github:NixOS/nix/2.24.0` or `...?ref=v0.11.0`;
* inputs you mark with a comment, on the url's line or the line above it:

  ```nix
  hyprland.url = "github:hyprwm/Hyprland"; # track: latest-release
  # track: latest-release prefix=v1.20.
  kubernetes.url = "github:kubernetes/kubernetes/v1.20.0";
  ```

Everything else is left alone: inputs that follow a branch (`nixos-unstable`, `release-24.05`,
no ref at all), inputs pinned with `?rev=` or a commit hash, and anything that is not `github:`.
`--input NAME` (repeatable) restricts the run to the inputs you name, and `--all` looks at every
`github:` input.

## What "latest" means

1. The repository's latest GitHub release, exactly what
   `github.com/owner/repo/releases/latest` shows: drafts and pre-releases never count.
2. If the repository publishes no releases (NixOS/nix itself only pushes tags), the highest
   stable version tag. Tags with a suffix (`2.32.0-rc1`, `2.4pre`) are skipped, and only tags
   shaped like your current pin compete (`v1.2.0` is compared with other `v…` tags).
3. With `prefix=…` on the marker, the same two steps restricted to tags that start with the
   prefix. Use it to stay on one release line (`prefix=v1.20.`) or to pick one component of a
   monorepo (`prefix=cli-`).

A pin that is already *ahead* of the latest stable release (you are on a release candidate, or
upstream marked a backport as latest) is reported as `ahead` and never downgraded.

## Commands

`flake-latest-release check [PATH] [--json] [--input NAME]... [--all]`

Prints the table above, or JSON with `name`, `owner`, `repo`, `line`, `current`, `latest`,
`outdated`, `status`, `source` (`release` or `tag`) and `url`. Exit code 0 if everything is
current, 1 if something is outdated, 2 on an error (rate limit, network, missing repository).
It never writes anything.

`flake-latest-release update [PATH] [--dry-run] [--lock] [--input NAME]... [--all]`

Rewrites the ref of each outdated input in `flake.nix`. Only the characters between the url's
quotes change; formatting, comments and line endings stay byte for byte. `?ref=` urls keep that
form and other parameters such as `dir=` are kept. `--dry-run` prints a diff and writes
nothing. `--lock` then runs `nix flake update NAME` for each changed input (Nix 2.19 or newer).

`PATH` is a `flake.nix` or its directory, default `.`.

Set `GITHUB_TOKEN` (or `GH_TOKEN`) to raise GitHub's API limit from 60 requests an hour. One
input costs one request, or a few for repositories without releases.

## Get notified

A scheduled workflow that fails, and so emails you, when a pinned input has a new release:

```yaml
name: flake releases
on:
  schedule:
    - cron: "0 7 * * 1"
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: AhmedB03/flake-latest-release
          path: .flake-latest-release
      - run: PYTHONPATH=.flake-latest-release python3 -m flake_latest_release check .
        env:
          GITHUB_TOKEN: ${{ github.token }}
```

## Limits

* GitHub only, and only the `github:owner/repo` url form. `git+https://github.com/…` urls,
  the attribute form (`type = "github"; owner = …;`) and GitHub Enterprise (`?host=`) are not
  looked at.
* Inputs must be written literally under `inputs` in `flake.nix`, which Nix requires anyway.
* The tag fallback reads at most the 1,000 most recent tags of a repository.
* It does not read `flake.lock`; "current" is what `flake.nix` says.

## Related

[flake-edit](https://github.com/a-kenji/flake-edit) is a full flake input editor, and its
`update` command also bumps pinned inputs to newer tags. It orders tags by semver, which means
a pre-release of a higher version can win, and it does not look at GitHub releases. This tool
does one thing: stable releases only, a read-only `check` with an exit code for CI, and no
dependencies.

## Tests

```sh
pip install pytest && python -m pytest -q
```

## How this was made

The idea came from [Uncharted](https://github.com/AhmedB03/uncharted), which looks for
long-open, upvoted feature requests that a small outside tool could answer. The code was
written with AI models (Claude) and then checked against real flakes and real Nix: the parser
was compared with a dozen public `flake.nix` files, and `update --lock` was run under Nix
itself. Bug reports are welcome.
