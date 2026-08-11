#!/usr/bin/env python3
"""Report and optionally verify the exact skill checkout being executed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


SKILL_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "https://github.com/sk-zluri/make-interactive-pdfs"
PROVENANCE_FILES = (
    "VERSION",
    "SKILL.md",
    "requirements.txt",
    "requirements-pixel.txt",
    "scripts/make_interactive_pdf.py",
    "scripts/verify_interactive_pdf.py",
    "scripts/run_isolated.py",
    "scripts/skill_provenance.py",
)
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
GITHUB_SCP_RE = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", re.I)
OWNER_FILE = ".make-interactive-pdfs-environment.json"


def normalize_repository_url(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().replace("\\", "/")
    scp_match = GITHUB_SCP_RE.fullmatch(normalized)
    if scp_match:
        owner, repository = scp_match.groups()
        return f"https://github.com/{owner}/{repository}".casefold()
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
        return None
    if parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme == "https" and (parsed.username or parsed.password or port is not None):
        return None
    if parsed.scheme == "ssh" and (parsed.username != "git" or port not in {None, 22}):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        return None
    return f"https://github.com/{owner}/{repository}".casefold()


def advertised_remote_head(repository_url: str) -> str | None:
    normalized = normalize_repository_url(repository_url)
    if not normalized:
        return None
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--exit-code", normalized, "HEAD"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip().split(maxsplit=1)[0] if completed.stdout.strip() else ""
    return commit.casefold() if FULL_COMMIT_RE.fullmatch(commit) else None


def git_output(skill_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(skill_root), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def file_hashes(skill_root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in PROVENANCE_FILES:
        path = skill_root / relative
        if path.is_file():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def combined_hash(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in sorted(hashes.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def collect_provenance(skill_root: Path = SKILL_ROOT) -> dict:
    skill_root = skill_root.resolve()
    reported_root = git_output(skill_root, "rev-parse", "--show-toplevel")
    git_root = Path(reported_root).resolve() if reported_root else None
    if git_root != skill_root:
        git_root = None

    raw_repository = git_output(skill_root, "remote", "get-url", "origin") if git_root else None
    repository = normalize_repository_url(raw_repository)
    commit = git_output(skill_root, "rev-parse", "HEAD") if git_root else None
    status = git_output(skill_root, "status", "--porcelain", "--untracked-files=all") if git_root else None
    expected_repository = normalize_repository_url(
        os.environ.get("MAKE_INTERACTIVE_PDFS_EXPECTED_REPOSITORY")
    )
    expected_commit = os.environ.get("MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT")
    advertised_head = os.environ.get("MAKE_INTERACTIVE_PDFS_ADVERTISED_HEAD")
    hashes = file_hashes(skill_root)
    version_path = skill_root / "VERSION"
    return {
        "schema_version": 1,
        "skill": "make-interactive-pdfs",
        "version": version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else None,
        "canonical_repository": CANONICAL_REPOSITORY,
        "git_repository": repository,
        "git_commit": commit,
        "git_dirty": None if not git_root or status is None else bool(status),
        "expected_repository": expected_repository,
        "repository_verified": repository == expected_repository if expected_repository else None,
        "expected_commit": expected_commit,
        "commit_verified": (
            (commit or "").casefold() == expected_commit.casefold() if expected_commit else None
        ),
        "advertised_remote_head": advertised_head,
        "remote_head_verified": (
            (commit or "").casefold() == advertised_head.casefold() if advertised_head else None
        ),
        "bundle_sha256": combined_hash(hashes),
        "files_sha256": hashes,
        "runtime": {
            "isolated": os.environ.get("MAKE_INTERACTIVE_PDFS_ISOLATED") == "1",
            "profile": os.environ.get("MAKE_INTERACTIVE_PDFS_PROFILE"),
            "environment": os.environ.get("MAKE_INTERACTIVE_PDFS_ENVIRONMENT"),
            "python": platform.python_version(),
        },
    }


def require_isolated_runtime(skill_root: Path = SKILL_ROOT) -> dict:
    """Reject direct/shared-Python execution and return validated provenance."""
    report = collect_provenance(skill_root)
    errors: list[str] = []
    declared_environment = report["runtime"].get("environment")
    if not report["runtime"]["isolated"] or not declared_environment:
        errors.append("This command must run through scripts/run_isolated.py")
    else:
        environment = Path(declared_environment).expanduser().resolve()
        prefix = Path(sys.prefix).resolve()
        base_prefix = Path(sys.base_prefix).resolve()
        if prefix != environment:
            errors.append("The active Python interpreter is outside the declared skill environment")
        if prefix == base_prefix:
            errors.append("The active Python interpreter is not an isolated virtual environment")

        configuration = environment / "pyvenv.cfg"
        configuration_text = (
            configuration.read_text(encoding="utf-8").casefold() if configuration.is_file() else ""
        )
        if "include-system-site-packages = false" not in configuration_text:
            errors.append("The skill environment must disable system site packages")

        owner_path = environment / OWNER_FILE
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            owner = None
        if not isinstance(owner, dict) or owner.get("managed_by") != "make-interactive-pdfs":
            errors.append("The active virtual environment is not owned by this skill")
        else:
            try:
                recorded_environment = Path(str(owner["environment"])).expanduser().resolve()
            except (KeyError, OSError, RuntimeError, ValueError):
                recorded_environment = None
            if recorded_environment != environment:
                errors.append("The environment ownership marker does not match the active environment")

    expected_repository = os.environ.get("MAKE_INTERACTIVE_PDFS_EXPECTED_REPOSITORY")
    expected_commit = os.environ.get("MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT")
    if expected_repository or expected_commit:
        errors.extend(
            validate_provenance(
                report,
                expected_repository=expected_repository,
                expected_commit=expected_commit,
                require_git=True,
                require_clean=True,
            )
        )
    if errors:
        raise RuntimeError("; ".join(dict.fromkeys(errors)))
    return report


def validate_provenance(
    report: dict,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    require_git: bool = False,
    require_clean: bool = False,
) -> list[str]:
    errors: list[str] = []
    if require_git and not report["git_commit"]:
        errors.append("A Git checkout is required, but this skill copy has no Git metadata")
    if expected_repository:
        expected = normalize_repository_url(expected_repository)
        actual = normalize_repository_url(report["git_repository"])
        if not expected:
            errors.append("Expected repository must be a supported GitHub HTTPS or SSH URL")
        elif not report["git_commit"]:
            errors.append("Cannot verify the requested repository without Git metadata")
        elif actual != expected:
            errors.append(f"Repository mismatch: expected {expected!r}, found {actual!r}")
        else:
            remote_head = advertised_remote_head(expected)
            report["advertised_remote_head"] = remote_head
            report["remote_head_verified"] = (
                remote_head == (report["git_commit"] or "").casefold() if remote_head else False
            )
            if not remote_head:
                errors.append("Could not verify the advertised HEAD commit from the requested repository")
            elif not report["remote_head_verified"]:
                errors.append(
                    f"Local HEAD {report['git_commit']!r} does not match advertised remote HEAD {remote_head!r}"
                )
    if expected_commit:
        normalized_commit = expected_commit.casefold()
        if not FULL_COMMIT_RE.fullmatch(normalized_commit):
            errors.append("Expected commit must be a full 40-character hexadecimal Git commit ID")
        elif (report["git_commit"] or "").casefold() != normalized_commit:
            errors.append(
                f"Commit mismatch: expected {normalized_commit}, found {report['git_commit']!r}"
            )
    if require_clean and report["git_dirty"] is not False:
        errors.append("A clean Git checkout is required")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--expect-repository", help="Fail unless origin matches this repository URL")
    parser.add_argument("--expect-commit", help="Fail unless HEAD equals this full Git commit ID")
    parser.add_argument("--require-git", action="store_true", help="Fail outside a Git checkout")
    parser.add_argument("--require-clean", action="store_true", help="Fail when the checkout is dirty")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = collect_provenance()
    errors = validate_provenance(
        report,
        expected_repository=args.expect_repository,
        expected_commit=args.expect_commit,
        require_git=args.require_git,
        require_clean=args.require_clean,
    )
    report["expected_repository"] = normalize_repository_url(args.expect_repository)
    report["expected_commit"] = args.expect_commit.casefold() if args.expect_commit else None
    report["repository_verified"] = (
        normalize_repository_url(report["git_repository"])
        == normalize_repository_url(args.expect_repository)
        if args.expect_repository
        else None
    )
    report["commit_verified"] = (
        (report["git_commit"] or "").casefold() == args.expect_commit.casefold()
        if args.expect_commit
        else None
    )
    report["errors"] = errors
    report["status"] = "FAIL" if errors else "PASS"
    print(json.dumps(report, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
