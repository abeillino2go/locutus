from __future__ import annotations

import argparse
import fnmatch
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
        raise RuntimeError(f"Config not found: {path}. Copy config.example.toml to this location.")
    if not isinstance(config.get("database", {}), dict):
        raise RuntimeError("Invalid config: [database.<name>] sections are required.")
    return config


def db_dir(config: dict[str, Any]) -> Path:
    raw = config.get("global", {}).get("database_dir", "~/.local/share/locutus")
    return Path(raw).expanduser()


def db_path(config: dict[str, Any], name: str) -> Path:
    return db_dir(config) / f"{name}.db"


def validate_name(name: str, databases: dict[str, Any]) -> None:
    if name not in databases:
        choices = ", ".join(sorted(databases)) or "(none configured)"
        raise RuntimeError(f"Unknown database '{name}'. Configured: {choices}")


def executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required command '{name}' not found in PATH. Install plocate.")
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
        for root in configured_roots(section):
            state = "OK" if root.is_dir() else "MISSING"
            print(f"  {state:7} {root}")
    return 0


def command_list(config: dict[str, Any]) -> int:
    for name in sorted(config.get("database", {})):
        path = db_path(config, name)
        state = f"{path.stat().st_size} bytes" if path.is_file() else "not built"
        print(f"{name:20} {state:14} {path}")
    return 0


def update_one(config: dict[str, Any], name: str) -> int:
    databases = config.get("database", {})
    validate_name(name, databases)
    section = databases[name]
    roots = configured_roots(section)
    skip_unavailable = config.get("global", {}).get("skip_unavailable", True)
    existing = [p for p in roots if p.is_dir()]
    missing = [p for p in roots if not p.is_dir()]
    for path in missing:
        print(f"WARNING: unavailable path: {path}", file=sys.stderr)
    if missing and not skip_unavailable:
        print(f"ERROR: refusing update of '{name}' because a path is unavailable", file=sys.stderr)
        return 1
    if not existing:
        print(f"ERROR: no accessible roots for '{name}'; preserving existing DB", file=sys.stderr)
        return 1

    target_dir = db_dir(config)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = db_path(config, name)
    fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".db.tmp", dir=target_dir)
    os.close(fd)
    temp = Path(temp_name)
    temp.unlink()  # updatedb creates the output database itself

    updatedb = executable("updatedb")
    overall = 0
    try:
        # Build each configured root into an intermediate database, then merge
        # path records by asking updatedb to scan all roots where supported.
        # plocate's updatedb accepts one -U root, so use one DB per root and
        # combine them with updatedb's --add-prunepaths? Instead, scan each root
        # sequentially into a shared output is not supported. Use a temporary
        # staging directory list and invoke updatedb once per root, merging via
        # plocate's database merge utility is not portable. Therefore create one
        # index per named DB from a common ancestor when roots share one, or use
        # a generated find list through updatedb --files0-from where available.
        files0 = target_dir / f".{name}.paths0"
        try:
            with files0.open("wb") as stream:
                for root in existing:
                    stream.write(os.fsencode(str(root.resolve())) + b"\0")
            cmd = [updatedb, "--database-root", "/", "--output", str(temp), "--files0-from", str(files0)]
            probe = subprocess.run([updatedb, "--help"], text=True, capture_output=True)
            help_text = probe.stdout + probe.stderr
            if "--files0-from" not in help_text:
                if len(existing) != 1:
                    raise RuntimeError("This updatedb does not support --files0-from; configure one root per database for now.")
                cmd = [updatedb, "-U", str(existing[0]), "-o", str(temp)]
            result = subprocess.run(cmd, check=False)
            overall = result.returncode
        finally:
            files0.unlink(missing_ok=True)

        if overall != 0:
            print(f"ERROR: updatedb failed for '{name}' (exit {overall}); old database preserved", file=sys.stderr)
            return overall
        if not temp.is_file() or temp.stat().st_size == 0:
            print(f"ERROR: updatedb produced no database; old database preserved", file=sys.stderr)
            return 1
        os.replace(temp, target)
        print(f"Updated {name}: {target}")
        return 0
    finally:
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
        paths = [db_path(config, name) for name in sorted(databases)]
        paths = [p for p in paths if p.is_file()]
        if not paths:
            print("No Locutus databases exist yet.", file=sys.stderr)
            return 1
        for path in paths:
            command.extend(["-d", str(path)])
    elif args.db:
        validate_name(args.db, databases)
        path = db_path(config, args.db)
        if not path.is_file():
            print(f"Database not found: {path}. Run locutus update -db {args.db}", file=sys.stderr)
            return 1
        command.extend(["-d", str(path)])
    command.extend(args.pattern)
    return subprocess.run(command, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="locutus", description="Manage named plocate indexes")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="configuration file")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="list configured databases")
    sub.add_parser("check", help="check tools and configured roots")

    update = sub.add_parser("update", help="build or refresh databases")
    update.add_argument("-db", dest="db")
    update.add_argument("--all", action="store_true")

    find = sub.add_parser("find", help="search a database (default: system plocate DB)")
    find.add_argument("-db", dest="db", help="database name or 'all'")
    find.add_argument("-i", "--ignore-case", action="store_true")
    find.add_argument("-r", "--regex", action="store_true")
    find.add_argument("-n", "--limit", type=int)
    find.add_argument("pattern", nargs="+")

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
    except (RuntimeError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"locutus: error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
