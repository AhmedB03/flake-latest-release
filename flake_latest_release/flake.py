"""Find `github:` inputs in flake.nix text and rewrite their refs in place.

flake.nix is scanned with a small tokenizer for the subset of Nix that the
`inputs` section may use (Nix requires it to be literal attribute sets and
strings). Comments, strings, and nested braces are understood, so an
`inputs.x.url` inside `outputs` or inside a string is never mistaken for an
input. Rewrites replace only the characters between the url's quotes.
"""

import bisect
import re
from dataclasses import dataclass, field
from typing import Optional

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*")
MARKER_RE = re.compile(r"track:\s*latest-release(?:\s+prefix=(\S+))?")
GITHUB_URL_RE = re.compile(r"^github:([^/?#]+)/([^/?#]+)(?:/([^?#]+))?(?:\?([^#]*))?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_PREFIX = "refs/tags/"
VERSION_RE = re.compile(r"^([A-Za-z_-]*)(\d+(?:\.\d+)*)$")


def parse_version(tag: Optional[str]):
    """Split a stable version tag into (family, numbers): `v1.2.3` ->
    ("v", (1, 2, 3)), `release-24.05` -> ("release-", (24, 5)). Returns None
    for anything else, including pre-releases such as `v2.0.0-rc1`."""
    m = VERSION_RE.match(tag or "")
    if not m:
        return None
    return m.group(1), tuple(int(x) for x in m.group(2).split("."))


@dataclass
class Token:
    kind: str  # ident, string, comment, or one of "{ } = ; ." or "other"
    value: str
    start: int  # offset of the token (for strings: of the content)
    end: int
    interpolated: bool = False


@dataclass
class FlakeInput:
    name: str
    owner: str
    repo: str
    ref: Optional[str]  # tag or branch as written, without refs/tags/
    rev_pinned: bool
    tracked: bool  # has a `# track: latest-release` marker
    prefix: Optional[str]  # prefix= value of the marker
    line: int  # 1-based line of the url string
    url: str
    start: int  # offsets of the url text between its quotes
    end: int
    path_ref: Optional[str] = None
    params: list = field(default_factory=list)  # [(key, value)] in order

    @property
    def version_pinned(self) -> bool:
        """The ref is a plain version tag such as `2.24.0` or `v0.41`.
        `nixos-24.05` and `release-24.05` do not count: by convention those
        are release branches, which already move on their own."""
        parsed = parse_version(self.ref)
        return not self.rev_pinned and parsed is not None and parsed[0] in ("", "v")

    def url_with_ref(self, tag: str) -> str:
        """The same url, pointing at `tag`. Other query parameters are kept."""
        base = f"github:{self.owner}/{self.repo}"
        params = list(self.params)
        keys = [k for k, _ in params]
        if "ref" in keys:
            i = keys.index("ref")
            old = params[i][1]
            new = TAG_PREFIX + tag if old.startswith(TAG_PREFIX) else tag
            params[i] = ("ref", new)
        elif "/" in tag:
            # a tag with a slash cannot go in the path form
            params.insert(0, ("ref", tag))
        else:
            base += "/" + tag
        if params:
            base += "?" + "&".join(k if v is None else f"{k}={v}" for k, v in params)
        return base


# ---------------------------------------------------------------- tokenizer

def _skip_dq(text, i):
    """`i` is just past an opening `"`. Return (index past closing quote, interpolated)."""
    n = len(text)
    interp = False
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1, interp
        elif c == "$" and text.startswith("${", i):
            interp = True
            i = _skip_code(text, i + 2)
        else:
            i += 1
    return n, interp


def _skip_indented(text, i):
    """`i` is just past an opening `''`. Return index past the closing `''`."""
    n = len(text)
    while i < n:
        if text.startswith("''", i):
            nxt = text[i + 2:i + 3]
            if nxt in ("'", "$"):
                i += 3
            elif nxt == "\\":
                i += 4
            else:
                return i + 2
        elif text.startswith("${", i):
            i = _skip_code(text, i + 2)
        else:
            i += 1
    return n


def _skip_code(text, i):
    """`i` is just past a `{`. Return index past the matching `}`."""
    n = len(text)
    depth = 1
    while i < n:
        c = text[i]
        if c == '"':
            i, _ = _skip_dq(text, i + 1)
        elif text.startswith("''", i):
            i = _skip_indented(text, i + 2)
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return n


def tokenize(text: str) -> list:
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            tokens.append(Token("comment", text[i:j], i, j))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            tokens.append(Token("comment", text[i:j], i, j))
            i = j
        elif c == '"':
            j, interp = _skip_dq(text, i + 1)
            tokens.append(Token("string", text[i + 1:j - 1], i + 1, j - 1, interp))
            i = j
        elif text.startswith("''", i):
            j = _skip_indented(text, i + 2)
            tokens.append(Token("other", text[i:j], i, j))
            i = j
        elif c in "{};":
            tokens.append(Token(c, c, i, i + 1))
            i += 1
        elif c == "=" and text[i + 1:i + 2] != "=":
            tokens.append(Token("=", c, i, i + 1))
            i += 1
        elif c == "." and text[i + 1:i + 2] != ".":
            tokens.append(Token(".", c, i, i + 1))
            i += 1
        else:
            m = IDENT_RE.match(text, i)
            if m:
                tokens.append(Token("ident", m.group(), i, m.end()))
                i = m.end()
            else:
                j = i + 1
                while j < n and text[j] in "=.":
                    j += 1  # `==`, `...`
                tokens.append(Token("other", text[i:j], i, j))
                i = j
    return tokens


# ------------------------------------------------------------------ parsing

def _string_bindings(code):
    """Yield (attribute path, string token) for every `a.b.c = "...";` that
    sits in the literal attribute sets reachable from the top of the file."""
    stack = []  # one entry per open brace: list of names, or None if opaque
    seen_root = False
    stmt_start = False
    i, n = 0, len(code)
    while i < n:
        t = code[i]
        if t.kind == "{":
            if not seen_root and not stack:
                stack.append([])
                seen_root = True
            else:
                stack.append(None)
            stmt_start = True
            i += 1
        elif t.kind == "}":
            if stack:
                stack.pop()
            stmt_start = False
            i += 1
        elif t.kind == ";":
            stmt_start = True
            i += 1
        elif stmt_start and t.kind in ("ident", "string") and None not in stack:
            stmt_start = False
            path, j = [], i
            while j < n and (code[j].kind == "ident"
                             or (code[j].kind == "string" and not code[j].interpolated)):
                path.append(code[j].value)
                j += 1
                if j < n and code[j].kind == ".":
                    j += 1
                else:
                    break
            else:
                path = []
            if not path or j >= n or code[j].kind != "=":
                i += 1
                continue
            j += 1
            if j + 1 < n and code[j].kind == "ident" and code[j].value == "rec" \
                    and code[j + 1].kind == "{":
                j += 1
            if j < n and code[j].kind == "{":
                stack.append(path)
                stmt_start = True
                i = j + 1
                continue
            if j + 1 < n and code[j].kind == "string" and not code[j].interpolated \
                    and code[j + 1].kind == ";":
                full = [name for frame in stack for name in frame] + path
                yield full, code[j]
            i = j
        else:
            stmt_start = False
            i += 1


def _marker_for(tokens, line_of, text, tok):
    """Marker comment on the url's line (after it), or alone on the line above."""
    line = line_of(tok.start)
    for c in tokens:
        if c.kind != "comment":
            continue
        cl = line_of(c.start)
        same_line = cl == line and c.start > tok.end
        line_start = text.rfind("\n", 0, c.start) + 1
        above = cl == line - 1 and not text[line_start:c.start].strip()
        if same_line or above:
            m = MARKER_RE.search(c.value)
            if m:
                return True, m.group(1)
    return False, None


def parse_github_url(url: str):
    """Split a `github:` flake url. Returns None if it is not one."""
    m = GITHUB_URL_RE.match(url)
    if not m:
        return None
    owner, repo, path_ref, query = m.groups()
    params = []
    for part in (query or "").split("&"):
        if part:
            k, sep, v = part.partition("=")
            params.append((k, v if sep else None))
    return owner, repo, path_ref, params


def parse_flake(text: str) -> list:
    """Return the `github:` inputs of a flake.nix, in file order."""
    tokens = tokenize(text)
    code = [t for t in tokens if t.kind != "comment"]
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_of(offset):
        return bisect.bisect_left(newlines, offset) + 1

    inputs = []
    for path, tok in _string_bindings(code):
        if len(path) != 3 or path[0] != "inputs" or path[2] != "url":
            continue
        parsed = parse_github_url(tok.value)
        if parsed is None:
            continue
        owner, repo, path_ref, params = parsed
        qs = {k: v for k, v in params}
        ref = qs.get("ref") or path_ref
        rev_pinned = "rev" in qs or bool(path_ref and SHA_RE.match(path_ref))
        if ref and ref.startswith(TAG_PREFIX):
            ref = ref[len(TAG_PREFIX):]
        tracked, prefix = _marker_for(tokens, line_of, text, tok)
        inputs.append(FlakeInput(
            name=path[1], owner=owner, repo=repo, ref=ref, rev_pinned=rev_pinned,
            tracked=tracked, prefix=prefix, line=line_of(tok.start), url=tok.value,
            start=tok.start, end=tok.end, path_ref=path_ref, params=params))
    return inputs


def rewrite_flake_text(text: str, changes: list) -> str:
    """Apply [(FlakeInput, new tag)] to `text`. Only the characters between
    each url's quotes change; every other byte is preserved."""
    for inp, tag in sorted(changes, key=lambda c: c[0].start, reverse=True):
        text = text[:inp.start] + inp.url_with_ref(tag) + text[inp.end:]
    return text
