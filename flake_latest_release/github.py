"""GitHub release and tag lookup. The only module that touches the network."""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .flake import parse_version

API = "https://api.github.com"
MAX_PAGES = 10  # look at up to 1000 releases or tags per repository


class NotFound(Exception):
    """HTTP 404: no published release, or no such repository."""


class GitHubError(Exception):
    """Any other failure talking to GitHub; the message is user-facing."""


@dataclass
class Release:
    tag: str
    url: Optional[str] = None
    source: str = "release"  # "release" or "tag"


def http_get_json(url: str, token: Optional[str] = None):
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "flake-latest-release"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
            hint = "" if token else "; set GITHUB_TOKEN for a higher limit"
            raise GitHubError(f"GitHub API rate limit reached{hint}") from e
        if e.code == 401:
            raise GitHubError("GitHub rejected the token in GITHUB_TOKEN") from e
        raise GitHubError(f"GitHub API returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise GitHubError(f"could not reach GitHub: {e}") from e


def _repo_api(owner, repo):
    return f"{API}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def pick_release(releases: list, prefix: str) -> Optional[Release]:
    """Most recently published non-draft, non-prerelease whose tag starts
    with `prefix`, from one page of the releases list."""
    best = None
    for rel in releases:
        tag = rel.get("tag_name")
        if not tag or rel.get("draft") or rel.get("prerelease"):
            continue
        if not tag.startswith(prefix):
            continue
        key = rel.get("published_at") or ""
        if best is None or key > best[0]:
            best = (key, Release(tag, rel.get("html_url")))
    return best[1] if best else None


def latest_release(owner: str, repo: str, prefix: Optional[str] = None,
                   fetch=http_get_json, token: Optional[str] = None) -> Optional[Release]:
    """What github.com/<owner>/<repo>/releases/latest points at, or with
    `prefix`, the newest stable release whose tag starts with it.
    Returns None when there is no such release."""
    base = _repo_api(owner, repo) + "/releases"
    try:
        if prefix is None:
            data = fetch(f"{base}/latest", token)
            tag = data.get("tag_name")
            return Release(tag, data.get("html_url")) if tag else None
        for page in range(1, MAX_PAGES + 1):
            releases = fetch(f"{base}?per_page=100&page={page}", token)
            found = pick_release(releases, prefix)
            if found:
                return found
            if len(releases) < 100:
                return None
    except NotFound:
        return None
    return None


def pick_tag(names: list, prefix: Optional[str] = None,
             current: Optional[str] = None) -> Optional[str]:
    """Highest stable version tag. Pre-release tags (`-rc1`, `-pre`, ...) and
    non-version tags never qualify. With `prefix`, only tags starting with
    it; otherwise only tags in the same family as `current` (`v1.2` only
    competes with other `v...` tags), or plain and `v`-prefixed ones when
    `current` is not a version."""
    cur = parse_version(current)
    best = None
    for name in names:
        parsed = parse_version(name)
        if parsed is None:
            continue
        family, numbers = parsed
        if prefix is not None:
            if not name.startswith(prefix):
                continue
        elif cur is not None:
            if family != cur[0]:
                continue
        elif family not in ("", "v"):
            continue
        if best is None or numbers > best[0]:
            best = (numbers, name)
    return best[1] if best else None


def latest_tag(owner: str, repo: str, prefix: Optional[str] = None,
               current: Optional[str] = None, fetch=http_get_json,
               token: Optional[str] = None) -> Optional[Release]:
    """For repositories that tag versions without publishing GitHub releases
    (NixOS/nix, for one): the highest stable version tag, see `pick_tag`.
    Raises NotFound if the repository does not exist."""
    names = []
    for page in range(1, MAX_PAGES + 1):
        tags = fetch(f"{_repo_api(owner, repo)}/tags?per_page=100&page={page}", token)
        names.extend(t.get("name") for t in tags if t.get("name"))
        if len(tags) < 100:
            break
    tag = pick_tag(names, prefix, current)
    if tag is None:
        return None
    url = f"https://github.com/{owner}/{repo}/releases/tag/{urllib.parse.quote(tag)}"
    return Release(tag, url, "tag")


def latest(owner: str, repo: str, prefix: Optional[str] = None,
           current: Optional[str] = None, fetch=http_get_json,
           token: Optional[str] = None) -> Optional[Release]:
    """Latest GitHub release, falling back to the highest stable version tag
    when the repository publishes no (matching) release."""
    return (latest_release(owner, repo, prefix, fetch, token)
            or latest_tag(owner, repo, prefix, current, fetch, token))
