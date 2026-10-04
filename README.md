# Locutus

A small manager for independent `plocate` databases.

## Requirements

- Python 3.11+
- `plocate` (`plocate` and `updatedb` commands available in `PATH`)

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
locutus --help
```

## Configuration

Copy `config.example.toml` to `~/.config/locutus/config.toml` and adjust paths.
Database files default to `~/.local/share/locutus`.

```bash
locutus list
locutus check
locutus update -db calibre
locutus update --all
locutus find -db calibre --ignore-case HUXLEY
locutus find HUXLEY
```

`find` without `-db` searches the system plocate database. Use `-db all` to search
all configured Locutus databases.

Updates are written to a temporary database first. The existing database is
replaced only if `updatedb` exits successfully. A configured root must exist;
unavailable roots are skipped only when `skip_unavailable = true`.

## Notes

This first version intentionally uses only the Python standard library and invokes
the native plocate tools. Symlinks are not followed by default.
