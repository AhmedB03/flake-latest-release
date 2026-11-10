"""flake_latest_release: check or pin flake inputs against GitHub's latest release."""

from .flake import FlakeInput, parse_flake, parse_version, rewrite_flake_text
from .github import Release, latest, latest_release, latest_tag, pick_release, pick_tag
from .cli import main

__all__ = ["FlakeInput", "Release", "parse_flake", "parse_version",
           "rewrite_flake_text", "latest", "latest_release", "latest_tag",
           "pick_release", "pick_tag", "main"]
