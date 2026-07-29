from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


FORBIDDEN_NAMES = (
    ".git", "node_modules", "__pycache__", ".pytest_cache", "browser_profiles",
    "cookies", "logs", "test_report", "license_admin", ".env",
)
SECRET_PATTERNS = {
    "PEM private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Aliyun AccessKey": re.compile(rb"\bLTAI[A-Za-z0-9]{12,}\b"),
    "GitHub token": re.compile(rb"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    "JWT token": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
}
ABSOLUTE_MARKERS = (
    r"D:\科技".encode("utf-8"),
    r"D:\科技".encode("utf-16le"),
    rb"C:\Users\Administrator",
    "C:\\Users\\Administrator".encode("utf-16le"),
)
GENERIC_TERMS = (
    "api_key", "client_secret", "access_token", "refresh_token", "password",
    "cookie", "authorization", "bearer", "private_key", "session_secret",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    files = [path for path in root.rglob("*") if path.is_file()]
    findings: list[dict] = []
    generic_hits: dict[str, list[str]] = {term: [] for term in GENERIC_TERMS}
    for path in files:
        relative = path.relative_to(root).as_posix()
        lower = relative.lower()
        if any(mark in lower for mark in FORBIDDEN_NAMES) or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".py", ".pyc"}:
            findings.append({"type": "forbidden_entry", "file": relative})
        data = path.read_bytes()
        third_party_runtime = relative.startswith("browser/chromium/") or relative.startswith("runtime/ffmpeg/")
        if not third_party_runtime:
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    findings.append({"type": label, "file": relative})
        if any(marker in data for marker in ABSOLUTE_MARKERS):
            findings.append({"type": "developer_absolute_path", "file": relative})
        # Generic terms are inventory-only: third-party binaries and program labels
        # legitimately contain them. They are never treated as secret proof alone.
        lowered = data.lower()
        for term in GENERIC_TERMS:
            if term.encode() in lowered and len(generic_hits[term]) < 20:
                generic_hits[term].append(relative)
    result = {
        "root": str(root),
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "sha256_tree": hashlib.sha256("\n".join(sorted(path.relative_to(root).as_posix() for path in files)).encode()).hexdigest(),
        "blocking_findings": findings,
        "generic_keyword_inventory": {key: value for key, value in generic_hits.items() if value},
        "passed": not findings,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
