"""Operator commands: python -m app.cli create-user | backup | retention | models"""

import argparse
import getpass
import sys

from . import auth, db
from .config import get_config


def create_user(args):
    password = args.password or getpass.getpass("Password (min 10 characters): ")
    with db.connect() as conn:
        if db.one(conn, "SELECT id FROM users WHERE username = ?", (args.username,)):
            sys.exit(f"User {args.username} already exists.")
        user_id = db.insert(conn, "users", {"username": args.username, "name": args.name or args.username,
                                            "password_hash": auth.hash_password(password), "role": args.role,
                                            "created_at": db.now()})
        db.audit(conn, None, "user.created_cli", "user", user_id, role=args.role)
    print(f"Created {args.role} {args.username}.")


def backup(args):
    from .routes.admin import make_backup

    print(make_backup(include_images=not args.db_only))


def retention(args):
    from .routes.admin import apply_retention

    rows = apply_retention(args.days, dry_run=not args.apply)
    verb = "Deleted" if args.apply else "Would delete"
    print(f"{verb} {len(rows)} closed incident(s): " + ", ".join(f"#{r['id']} {r['name']}" for r in rows))


def list_models(_args):
    from .services.models import available_models

    print(f"Models folder: {get_config().models_dir}")
    for m in available_models():
        print(f"  {m['name']}  {m['size_mb']} MB  task={m['task']}  classes={m['classes']}")


def seed_demo(args):
    from .demo_seed import seed

    seed(detector=args.detector, if_empty=args.if_empty)


def main(argv=None):
    from .config import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("create-user", help="Add a user (e.g. recover a lost admin login)")
    p.add_argument("username")
    p.add_argument("--role", choices=auth.ROLES, default="reviewer")
    p.add_argument("--name", default="")
    p.add_argument("--password", help="Prompted when omitted (recommended)")
    p.set_defaults(func=create_user)
    p = sub.add_parser("backup", help="Write a zip backup to DATA_DIR/backups")
    p.add_argument("--db-only", action="store_true")
    p.set_defaults(func=backup)
    p = sub.add_parser("retention", help="Delete closed incidents older than N days")
    p.add_argument("days", type=int)
    p.add_argument("--apply", action="store_true", help="Actually delete (default is a dry run)")
    p.set_defaults(func=retention)
    p = sub.add_parser("seed-demo", help="Create the Hurricane Ian judge-demo incident from bundled NOAA imagery")
    p.add_argument("--detector", choices=("claude", "demo"), default="claude")
    p.add_argument("--if-empty", action="store_true", help="Do nothing if any incident already exists")
    p.set_defaults(func=seed_demo)
    p = sub.add_parser("models", help="List model weights available to the app")
    p.set_defaults(func=list_models)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
