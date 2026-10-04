from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "locutus" / "config.toml"


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        raise RuntimeError(
            f"Config not found: {path}. Copy config.example.toml to this location."
        )

    if not isinstance(config.get("database", {}), dict):
        raise RuntimeError("Invalid config: [database.<name>] sections are required.")

    return config


def db_dir(config: dict[str, Any]) -> Path:
    raw = config.get("global", {}).get(
        "database_dir",
        "~/.local/share/locutus",
    )
    return Path(raw).expanduser()


def db_path(config: dict[str, Any], name: str, index: int | None = None) -> Path:
    """
    Return the database path.

    With an index:
        media.01.db
        media.02.db
        ...

    Without an index this returns the legacy/logical path:
        media.db

    The logical path is retained for compatibility with callers that may
    still use db_path() directly. Actual per-path databases use the indexed
    form.
    """
    directory = db_dir(config)

    if index is None:
        return directory / f"{name}.db"

    return directory / f"{name}.{index:02d}.db"


def indexed_db_paths(config: dict[str, Any], name: str) -> list[Path]:
    """
    Return all existing per-path databases for a logical database name.

    Only files matching the two-digit convention are considered:
        name.01.db
        name.02.db
        ...
    """
    directory = db_dir(config)

    if not directory.is_dir():
        return []

    prefix = f"{name}."
    result: list[tuple[int, Path]] = []

    for path in directory.glob(f"{name}.[0-9][0-9].db"):
        if not path.is_file():
            continue

        suffix = path.name[len(prefix):-3]  # remove "name." and ".db"

        if suffix.isdigit():
            result.append((int(suffix), path))

    result.sort(key=lambda item: item[0])
    return [path for _, path in result]


def validate_name(name: str, databases: dict[str, Any]) -> None:
    if name not in databases:
        choices = ", ".join(sorted(databases)) or "(none configured)"
        raise RuntimeError(f"Unknown database '{name}'. Configured: {choices}")


def executable(name: str) -> str:
    found = shutil.which(name)

    if not found:
        raise RuntimeError(
            f"Required command '{name}' not found in PATH. Install plocate."
        )

    return found


def configured_roots(section: dict[str, Any]) -> list[Path]:
    paths = section.get("paths", [])

    if not isinstance(paths, list) or not paths:
        raise RuntimeError("Each database needs a non-empty 'paths' array.")

    return [Path(p).expanduser() for p in paths]


def command_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    print(f"Config: {args.config}")

    for tool in ("plocate", "updatedb"):
        print(f"{tool}: {shutil.which(tool) or 'NOT FOUND'}")

    for name, section in config.get("database", {}).items():
        print(f"[{name}]")

        for index, root in enumerate(configured_roots(section), start=1):
            state = "OK" if root.is_dir() else "MISSING"
            db = db_path(config, name, index)
            print(f"  {state:7} {root}")
            print(f"           DB: {db}")

    return 0


def command_list(config: dict[str, Any]) -> int:
    databases = config.get("database", {})

    for name in sorted(databases):
        paths = indexed_db_paths(config, name)

        if not paths:
            print(f"{name:20} {'not built':14} {db_dir(config) / (name + '.*.db')}")
            continue

        total_size = sum(path.stat().st_size for path in paths)

        print(
            f"{name:20} "
            f"{len(paths)} DB{'s' if len(paths) != 1 else ' '} "
            f"{total_size} bytes"
        )

        for path in paths:
            print(f"  {path.stat().st_size:12} bytes  {path}")

    return 0


def update_one(config: dict[str, Any], name: str) -> int:
    databases = config.get("database", {})
    validate_name(name, databases)

    section = databases[name]
    roots = configured_roots(section)

    skip_unavailable = config.get("global", {}).get(
        "skip_unavailable",
        True,
    )

    existing = [path for path in roots if path.is_dir()]
    missing = [path for path in roots if not path.is_dir()]

    for path in missing:
        print(
            f"WARNING: unavailable path: {path}",
            file=sys.stderr,
        )

    if missing and not skip_unavailable:
        print(
            f"ERROR: refusing update of '{name}' because a path is unavailable",
            file=sys.stderr,
        )
        return 1

    if not existing:
        print(
            f"ERROR: no accessible roots for '{name}'; preserving existing DB",
            file=sys.stderr,
        )
        return 1

    # With skip_unavailable=True we deliberately index only the currently
    # accessible paths. The numbering follows their order in the configured
    # paths list.
    #
    # This means that if path 01 is unavailable while path 02 is available,
    # path 02 will temporarily become database 01. This is the intended
    # position-based (variant A) behaviour.
    target_dir = db_dir(config)
    target_dir.mkdir(parents=True, exist_ok=True)

    updatedb = executable("updatedb")

    temporary: list[tuple[Path, Path]] = []

    try:
        # Build every database first. Nothing is replaced until ALL updates
        # have succeeded.
        for index, root in enumerate(existing, start=1):
            target = db_path(config, name, index)

            fd, temp_name = tempfile.mkstemp(
                prefix=f".{name}.{index:02d}.",
                suffix=".db.tmp",
                dir=target_dir,
            )
            os.close(fd)

            temp = Path(temp_name)
            temp.unlink()

            temporary.append((temp, target))

            cmd = [
                updatedb,
                "-U",
                str(root),
                "-o",
                str(temp),
            ]

            print(
                f"Updating {name}.{index:02d}: {root}",
                file=sys.stderr,
            )

            result = subprocess.run(cmd, check=False)

            if result.returncode != 0:
                print(
                    f"ERROR: updatedb failed for '{name}' path {index} "
                    f"(exit {result.returncode}); old databases preserved",
                    file=sys.stderr,
                )
                return result.returncode

            if not temp.is_file() or temp.stat().st_size == 0:
                print(
                    f"ERROR: updatedb produced no database for "
                    f"'{name}.{index:02d}'; old databases preserved",
                    file=sys.stderr,
                )
                return 1

        # All databases have been built successfully.
        #
        # Replace the old databases only now. This means a failed updatedb
        # above cannot destroy an existing database.
        for temp, target in temporary:
            os.replace(temp, target)

        # Remove stale numbered databases left over from a previous config
        # with more paths than the current configuration.
        current_targets = {target for _, target in temporary}

        for old_path in indexed_db_paths(config, name):
            if old_path not in current_targets:
                old_path.unlink()
                print(
                    f"Removed obsolete database: {old_path}",
                    file=sys.stderr,
                )

        print(
            f"Updated {name}: {len(temporary)} database"
            f"{'' if len(temporary) == 1 else 's'}"
        )

        return 0

    finally:
        # Remove any temporary files that still exist.
        for temp, _ in temporary:
            temp.unlink(missing_ok=True)


def command_find(args: argparse.Namespace, config: dict[str, Any]) -> int:
    databases = config.get("database", {})
    command = [executable("plocate")]

    if args.ignore_case:
        command.append("-i")

    if args.regex:
        command.append("-r")

    if args.limit is not None:
        command.extend(["-n", str(args.limit)])

    if args.db == "all":
        paths: list[Path] = []

        for name in sorted(databases):
            paths.extend(indexed_db_paths(config, name))

        if not paths:
            print(
                "No Locutus databases exist yet.",
                file=sys.stderr,
            )
            return 1

        for path in paths:
            command.extend(["-d", str(path)])

    elif args.db:
        validate_name(args.db, databases)

        paths = indexed_db_paths(config, args.db)

        if not paths:
            print(
                f"Database not found: {db_dir(config) / (args.db + '.*.db')}. "
                f"Run locutus update -db {args.db}",
                file=sys.stderr,
            )
            return 1

        for path in paths:
            command.extend(["-d", str(path)])

    command.extend(args.pattern)

    return subprocess.run(
        command,
        check=False,
    ).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locutus",
        description="Manage named plocate indexes",
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="configuration file",
    )

    sub = parser.add_subparsers(
        dest="action",
        required=True,
    )

    sub.add_parser(
        "list",
        help="list configured databases",
    )

    sub.add_parser(
        "check",
        help="check tools and configured roots",
    )

    update = sub.add_parser(
        "update",
        help="build or refresh databases",
    )

    update.add_argument(
        "-db",
        dest="db",
    )

    update.add_argument(
        "--all",
        action="store_true",
    )

    find = sub.add_parser(
        "find",
        help="search a database (default: system plocate DB)",
    )

    find.add_argument(
        "-db",
        dest="db",
        help="database name or 'all'",
    )

    find.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
    )

    find.add_argument(
        "-r",
        "--regex",
        action="store_true",
    )

    find.add_argument(
        "-n",
        "--limit",
        type=int,
    )

    find.add_argument(
        "pattern",
        nargs="+",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config(args.config)

        if args.action == "list":
            code = command_list(config)

        elif args.action == "check":
            code = command_check(args, config)

        elif args.action == "find":
            code = command_find(args, config)

        elif args.action == "update":
            databases = config.get("database", {})

            if args.all:
                if args.db:
                    parser.error("Use either -db NAME or --all")

                code = 0

                for name in sorted(databases):
                    result = update_one(config, name)

                    if result:
                        code = result

            elif args.db:
                code = update_one(config, args.db)

            else:
                parser.error("update requires -db NAME or --all")

        else:
            code = 2

        raise SystemExit(code)

    except (
        RuntimeError,
        OSError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(
            f"locutus: error: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()