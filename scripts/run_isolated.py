#!/usr/bin/env python3
"""Run a bundled PDF command in a dedicated, reusable virtual environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

from skill_provenance import collect_provenance, normalize_repository_url, validate_provenance


SKILL_ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {
    "make": SKILL_ROOT / "scripts" / "make_interactive_pdf.py",
    "verify": SKILL_ROOT / "scripts" / "verify_interactive_pdf.py",
}
PIXEL_ARGUMENTS = {"--pixel-compare", "--save-renders", "--render-pages", "--render-dir"}
OWNER_FILE = ".make-interactive-pdfs-environment.json"


def environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirement_profile(command: str, arguments: list[str]) -> tuple[str, list[Path]]:
    uses_pixels = command == "verify" and any(
        argument in PIXEL_ARGUMENTS or any(argument.startswith(f"{name}=") for name in PIXEL_ARGUMENTS)
        for argument in arguments
    )
    files = [SKILL_ROOT / "requirements.txt"]
    if uses_pixels:
        files.append(SKILL_ROOT / "requirements-pixel.txt")
    return ("pixel" if uses_pixels else "core"), files


def requirement_digest(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def isolated_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "PIP_PREFIX", "PIP_TARGET", "PIP_USER"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def dependencies_importable(python: Path, profile: str) -> bool:
    modules = ["pypdf", "pdfplumber"]
    if profile == "pixel":
        modules.append("pymupdf")
    completed = subprocess.run(
        [str(python), "-c", "; ".join(f"import {module}" for module in modules)],
        check=False,
        env=isolated_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def ensure_environment(environment: Path, profile: str, requirement_files: list[Path]) -> Path:
    python = environment_python(environment)
    owner_path = environment / OWNER_FILE
    created = False
    if not python.is_file():
        if environment.exists() and any(environment.iterdir()):
            raise RuntimeError(
                f"Refusing to modify a pre-existing or incomplete environment without an interpreter: {environment}"
            )
        print(f"Creating isolated environment: {environment}", file=sys.stderr)
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
        created = True
    if not python.is_file():
        raise RuntimeError(f"Virtual environment did not create a Python interpreter: {python}")
    if not created:
        if not owner_path.is_file():
            raise RuntimeError(f"Refusing to modify an environment not owned by this skill: {environment}")
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid environment ownership marker: {owner_path}") from exc
        if owner.get("managed_by") != "make-interactive-pdfs":
            raise RuntimeError(f"Refusing to modify an environment owned by another tool: {environment}")

    configuration = environment / "pyvenv.cfg"
    configuration_text = configuration.read_text(encoding="utf-8").casefold() if configuration.is_file() else ""
    if "include-system-site-packages = false" not in configuration_text:
        raise RuntimeError("Dedicated environment must disable system site packages")
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import json, sys; print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))",
        ],
        text=True,
        capture_output=True,
        check=True,
        env=isolated_environment(),
    )
    interpreter = json.loads(probe.stdout)
    if Path(interpreter["prefix"]).resolve() != environment.resolve():
        raise RuntimeError("Refusing to install because the interpreter is outside the dedicated environment")
    if Path(interpreter["base_prefix"]).resolve() == environment.resolve():
        raise RuntimeError("Refusing to install because the interpreter is not an isolated virtual environment")
    if created:
        owner_path.write_text(
            json.dumps(
                {
                    "managed_by": "make-interactive-pdfs",
                    "environment": str(environment.resolve()),
                    "base_python": str(Path(sys.executable).resolve()),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    digest = requirement_digest(requirement_files)
    stamp = environment / f".make-interactive-pdfs-{profile}.sha256"
    installed_digest = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if installed_digest != digest or not dependencies_importable(python, profile):
        requirements = requirement_files[-1]
        print(f"Installing {profile} dependencies inside {environment}", file=sys.stderr)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--no-user",
                "-r",
                str(requirements),
            ],
            check=True,
            env=isolated_environment(),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        subprocess.run(
            [str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "check"],
            check=True,
            env=isolated_environment(),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        stamp.write_text(digest + "\n", encoding="utf-8")
    return python


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--venv-dir",
        default=str(SKILL_ROOT / ".venv"),
        help="Dedicated environment directory (default: .venv inside the skill checkout)",
    )
    parser.add_argument("--expect-repository", help="Require this exact Git repository origin")
    parser.add_argument("--expect-commit", help="Require this full Git commit ID")
    parser.add_argument("--require-clean", action="store_true", help="Require a clean skill checkout")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("command_arguments", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 or newer is required", file=sys.stderr)
        return 1
    args = build_parser().parse_args()
    provenance = collect_provenance()
    require_clean = args.require_clean or bool(args.expect_repository or args.expect_commit)
    provenance_errors = validate_provenance(
        provenance,
        expected_repository=args.expect_repository,
        expected_commit=args.expect_commit,
        require_git=bool(args.expect_repository or args.expect_commit),
        require_clean=require_clean,
    )
    if provenance_errors:
        failure = {
            "status": "FAIL",
            "expected_repository": normalize_repository_url(args.expect_repository),
            "expected_commit": args.expect_commit.casefold() if args.expect_commit else None,
            "skill_provenance": provenance,
            "errors": provenance_errors,
        }
        print(json.dumps(failure, indent=2), file=sys.stderr)
        return 1
    pinned_commit = args.expect_commit or (provenance["git_commit"] if args.expect_repository else None)
    arguments = list(args.command_arguments)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    profile, requirement_files = requirement_profile(args.command, arguments)
    try:
        python = ensure_environment(Path(args.venv_dir).expanduser().resolve(), profile, requirement_files)
        launch_provenance = collect_provenance()
        launch_errors = validate_provenance(
            launch_provenance,
            expected_repository=args.expect_repository,
            expected_commit=pinned_commit,
            require_git=bool(args.expect_repository or pinned_commit),
            require_clean=require_clean,
        )
        if launch_errors:
            raise RuntimeError("Provenance changed during environment setup: " + "; ".join(launch_errors))
        child_environment = isolated_environment()
        child_environment["MAKE_INTERACTIVE_PDFS_ISOLATED"] = "1"
        child_environment["MAKE_INTERACTIVE_PDFS_PROFILE"] = profile
        child_environment["MAKE_INTERACTIVE_PDFS_ENVIRONMENT"] = str(
            Path(args.venv_dir).expanduser().resolve()
        )
        if args.expect_repository:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_REPOSITORY"] = (
                normalize_repository_url(args.expect_repository) or ""
            )
        if args.expect_commit:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT"] = args.expect_commit.casefold()
        elif pinned_commit:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT"] = pinned_commit.casefold()
        if launch_provenance.get("advertised_remote_head"):
            child_environment["MAKE_INTERACTIVE_PDFS_ADVERTISED_HEAD"] = launch_provenance[
                "advertised_remote_head"
            ]
        completed = subprocess.run(
            [str(python), str(COMMANDS[args.command]), *arguments],
            check=False,
            env=child_environment,
        )
        final_provenance = collect_provenance()
        final_errors = validate_provenance(
            final_provenance,
            expected_repository=args.expect_repository,
            expected_commit=pinned_commit,
            require_git=bool(args.expect_repository or pinned_commit),
            require_clean=require_clean,
        )
        if final_errors:
            raise RuntimeError("Provenance changed during PDF processing: " + "; ".join(final_errors))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
