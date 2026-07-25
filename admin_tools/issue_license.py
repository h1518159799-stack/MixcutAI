"""Administrator-only Ed25519 license issuer. Never include in the user ZIP."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization


PRODUCT_ID = "mixcut-ai-windows"
TOKEN_PREFIX = "MXLIC1"
VALID_TYPES = ("month", "year", "permanent")
ROOT = Path(__file__).resolve().parent
DEFAULT_PRIVATE_KEY = Path(os.environ.get("MIXCUT_LICENSE_PRIVATE_KEY", ROOT / "keys" / "license_private_key.pem"))


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue(machine_id: str, license_type: str, expire_date: str = "", private_key_path: Path = DEFAULT_PRIVATE_KEY) -> str:
    if license_type not in VALID_TYPES:
        raise ValueError("unsupported license type")
    if not machine_id.startswith("MX-"):
        raise ValueError("machine id format is invalid")
    today = date.today()
    if license_type != "permanent":
        if not expire_date:
            expire_date = (today + timedelta(days=365 if license_type == "year" else 30)).isoformat()
        date.fromisoformat(expire_date)
    else:
        expire_date = ""
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    payload = {
        "schema": 1,
        "license_id": str(uuid.uuid4()),
        "product_id": PRODUCT_ID,
        "type": license_type,
        "machine_id": machine_id,
        "issued_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expire_date": expire_date,
        "nonce": secrets.token_urlsafe(18),
        "max_devices": 1,
        "transfer_policy": "server_approval",
    }
    encoded = canonical(payload)
    return f"{TOKEN_PREFIX}.{b64url(encoded)}.{b64url(private_key.sign(encoded))}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a signed Mixcut AI license")
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--type", choices=VALID_TYPES, default="month")
    parser.add_argument("--expire-date", default="")
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = issue(args.machine_id, args.type, args.expire_date, args.private_key)
    if args.output:
        args.output.write_text(token, encoding="utf-8")
        print(args.output.resolve())
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
