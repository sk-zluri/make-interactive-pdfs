#!/usr/bin/env python3
"""Run a bundled PDF command in a dedicated, reusable virtual environment."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

from skill_provenance import collect_provenance, normalize_repository_url, validate_provenance


SKILL_ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {
    "make": SKILL_ROOT / "scripts" / "make_interactive_pdf.py",
    "regression-test": SKILL_ROOT / "tests" / "regression_test.py",
    "self-test": SKILL_ROOT / "tests" / "self_test.py",
    "verify": SKILL_ROOT / "scripts" / "verify_interactive_pdf.py",
}
PIXEL_ARGUMENTS = {"--pixel-compare", "--save-renders", "--render-pages", "--render-dir"}
OWNER_FILE = ".make-interactive-pdfs-environment.json"


@contextlib.contextmanager
def environment_lock(environment: Path, timeout_seconds: float = 300.0):
    lock_root = Path(tempfile.gettempdir()) / "make-interactive-pdfs-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_identity = "environment\0" + str(environment.resolve())
    lock_name = hashlib.sha256(lock_identity.encode("utf-8")).hexdigest() + ".lock"
    lock_path = lock_root / lock_name
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for environment lock: {environment}")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_write_text(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirement_profile(command: str, arguments: list[str]) -> tuple[str, list[Path]]:
    if command in {"self-test", "regression-test"}:
        return (
            "dev",
            [
                SKILL_ROOT / "requirements.txt",
                SKILL_ROOT / "requirements-pixel.txt",
                SKILL_ROOT / "requirements-dev.txt",
            ],
        )
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


def required_versions(files: list[Path]) -> dict[str, tuple[str, str]]:
    expected: dict[str, tuple[str, str]] = {}
    for path in files:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "-r ")):
                continue
            if "==" not in line or ";" in line:
                raise RuntimeError(f"Requirement must be an unconditional exact pin: {line!r}")
            name, version = (part.strip() for part in line.split("==", 1))
            canonical = re.sub(r"[-_.]+", "-", name).casefold()
            if not canonical or not version:
                raise RuntimeError(f"Invalid pinned requirement: {line!r}")
            previous = expected.get(canonical)
            if previous and previous[1] != version:
                raise RuntimeError(f"Conflicting versions for {name}: {previous[1]} and {version}")
            expected[canonical] = (name, version)
    return expected


def installed_versions_match(python: Path, expected: dict[str, tuple[str, str]]) -> bool:
    query = {canonical: name for canonical, (name, _) in expected.items()}
    script = (
        "import importlib.metadata,json,sys; q=json.loads(sys.argv[1]); out={}; "
        "[(out.__setitem__(k, importlib.metadata.version(v))) for k,v in q.items()]; "
        "print(json.dumps(out, sort_keys=True))"
    )
    completed = subprocess.run(
        [str(python), "-c", script, json.dumps(query, sort_keys=True)],
        check=False,
        text=True,
        capture_output=True,
        env=isolated_environment(),
    )
    if completed.returncode != 0:
        return False
    try:
        actual = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    return all(actual.get(canonical) == version for canonical, (_, version) in expected.items())


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
    if profile in {"pixel", "dev"}:
        modules.append("pymupdf")
    if profile == "dev":
        modules.append("reportlab")
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
        try:
            recorded_environment = Path(str(owner["environment"])).resolve()
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Environment ownership marker has no valid path: {owner_path}") from exc
        if recorded_environment != environment.resolve():
            raise RuntimeError("Environment ownership marker does not match the selected environment")
        recorded_root = owner.get("skill_root")
        if recorded_root and Path(str(recorded_root)).resolve() != SKILL_ROOT.resolve():
            raise RuntimeError("Refusing to share an environment between different skill checkouts")

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
    owner_record = {
        "managed_by": "make-interactive-pdfs",
        "environment": str(environment.resolve()),
        "skill_root": str(SKILL_ROOT.resolve()),
        "skill_version": (SKILL_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "base_python": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
    }
    if created or owner != owner_record:
        atomic_write_text(owner_path, json.dumps(owner_record, indent=2) + "\n")

    digest = requirement_digest(requirement_files)
    expected_versions = required_versions(requirement_files)
    stamp = environment / f".make-interactive-pdfs-{profile}.sha256"
    installed_digest = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None
    if (
        installed_digest != digest
        or not dependencies_importable(python, profile)
        or not installed_versions_match(python, expected_versions)
    ):
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
        if not installed_versions_match(python, expected_versions):
            raise RuntimeError("Installed dependency versions do not match the exact release pins")
        atomic_write_text(stamp, digest + "\n")
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

    def execute_child(python: Path, environment_path: Path) -> subprocess.CompletedProcess:
        launch_provenance = collect_provenance()
        launch_errors = validate_provenance(
            launch_provenance,
            expected_repository=args.expect_repository,
            expected_commit=pinned_commit,
            require_git=bool(args.expect_repository or pinned_commit),
            require_clean=require_clean,
            verify_remote_head=False,
        )
        if launch_errors:
            raise RuntimeError("Provenance changed during environment setup: " + "; ".join(launch_errors))
        child_environment = isolated_environment()
        child_environment["MAKE_INTERACTIVE_PDFS_ISOLATED"] = "1"
        child_environment["MAKE_INTERACTIVE_PDFS_PROFILE"] = profile
        child_environment["MAKE_INTERACTIVE_PDFS_ENVIRONMENT"] = str(environment_path)
        if args.expect_repository:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_REPOSITORY"] = (
                normalize_repository_url(args.expect_repository) or ""
            )
        if args.expect_commit:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT"] = args.expect_commit.casefold()
        elif pinned_commit:
            child_environment["MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT"] = pinned_commit.casefold()
        if provenance.get("advertised_remote_head"):
            child_environment["MAKE_INTERACTIVE_PDFS_ADVERTISED_HEAD"] = provenance[
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
            verify_remote_head=False,
        )
        if final_errors:
            raise RuntimeError("Provenance changed during PDF processing: " + "; ".join(final_errors))
        return completed

    try:
        environment_path = Path(args.venv_dir).expanduser().resolve()
        if args.command in {"make", "verify"}:
            # Production commands retain the environment lock for the whole run,
            # preventing another profile install from mutating dependencies mid-PDF.
            with environment_lock(environment_path):
                python = ensure_environment(environment_path, profile, requirement_files)
                completed = execute_child(python, environment_path)
        else:
            # Test drivers recursively invoke this runner, so release the setup
            # lock before launching them; each nested production command locks.
            with environment_lock(environment_path):
                python = ensure_environment(environment_path, profile, requirement_files)
            completed = execute_child(python, environment_path)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
