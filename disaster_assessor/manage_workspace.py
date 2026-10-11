"""Host-administrator database backup using SQLite's consistent backup API."""
import argparse
import os
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.environ.get("DISASTER_DB_PATH", str(Path(__file__).parent / "data" / "workspace.sqlite3")))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source, destination = Path(args.database).resolve(), Path(args.output).resolve()
    if not source.is_file():
        parser.error("Database does not exist.")
    if destination.exists() or source == destination:
        parser.error("Choose a new backup file; existing files are not overwritten.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
    destination.chmod(0o600)
    print(f"Backup saved to {destination}")


if __name__ == "__main__":
    main()
