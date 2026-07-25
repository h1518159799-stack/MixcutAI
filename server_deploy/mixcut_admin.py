"""Root/operator CLI for MixcutAI cloud licensing."""

from __future__ import annotations

import argparse
from pathlib import Path

from license_server import VALID_TYPES, connect, create_card_keys, iso_utc, set_admin_password


def create_keys(kind: str, count: int, customer_id: str, max_devices: int, output: Path | None) -> None:
    with connect() as conn:
        rows = create_card_keys(conn, kind, count, customer_id, max_devices)
    text = "\n".join(rows) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        output.chmod(0o600)
        print(output)
    else:
        print(text, end="")


def list_keys(limit: int) -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.id,l.key_hint,l.type,l.status,l.machine_id,l.expire_date,l.max_devices,l.unbind_count,
               COALESCE(c.name,'') customer FROM license_keys l LEFT JOIN customers c ON c.id=l.customer_id
               ORDER BY l.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    for row in rows:
        print(" | ".join(str(row[key]) for key in row.keys()))


def set_status(license_id: str, status: str) -> None:
    with connect() as conn:
        result = conn.execute("UPDATE license_keys SET status=?,updated_at=? WHERE id=?", (status, iso_utc(), license_id))
        if result.rowcount != 1:
            raise SystemExit("license id not found")
    print(f"{license_id}: {status}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--type", choices=tuple(VALID_TYPES), default="month")
    create.add_argument("--count", type=int, default=1)
    create.add_argument("--customer-id", default="")
    create.add_argument("--max-devices", type=int, default=1)
    create.add_argument("--output", type=Path)
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=30)
    status = sub.add_parser("status")
    status.add_argument("license_id")
    status.add_argument("value", choices=("issued", "active", "frozen", "revoked", "disabled"))
    admin = sub.add_parser("admin-user")
    admin.add_argument("--username", default="admin")
    secret = admin.add_mutually_exclusive_group(required=True)
    secret.add_argument("--password")
    secret.add_argument("--password-file", type=Path)
    args = parser.parse_args()
    if args.command == "create":
        create_keys(args.type, args.count, args.customer_id, args.max_devices, args.output)
    elif args.command == "list":
        list_keys(max(1, min(1000, args.limit)))
    elif args.command == "status":
        set_status(args.license_id, args.value)
    else:
        password = args.password if args.password is not None else args.password_file.read_text(encoding="utf-8").strip()
        set_admin_password(args.username, password)
        print(f"admin user ready: {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
