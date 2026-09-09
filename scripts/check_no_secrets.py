#!/usr/bin/env python3
"""Fail when tracked files or Git history contain likely credentials.

Only finding labels and paths are printed. Matching content is never echoed.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = "scripts/check_no_secrets.py"
FORBIDDEN_NAMES = {
    ".env",
    "gtht-entry.json",
    "longbridge-client.json",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3"}
PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "assigned credential": re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
        rb"\s*[\"']?\s*[:=]\s*[\"']([^\"'\s]{16,})"
    ),
}


def run(*args: str) -> bytes:
    return subprocess.check_output(args, cwd=ROOT)


def tracked_files() -> list[str]:
    return [item.decode("utf-8", "replace") for item in run("git", "ls-files", "-z").split(b"\0") if item]


def history_blobs() -> list[tuple[str, str]]:
    rows = run("git", "rev-list", "--objects", "--all").splitlines()
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        oid, *path = row.decode("utf-8", "replace").split(" ", 1)
        if not path or oid in seen:
            continue
        seen.add(oid)
        if run("git", "cat-file", "-t", oid).strip() == b"blob":
            result.append((oid, path[0]))
    return result


def sensitive_path(path: str) -> bool:
    item = Path(path)
    return (
        ".local" in item.parts
        or item.name in FORBIDDEN_NAMES
        or (item.name.startswith(".env.") and item.name != ".env.example")
        or item.suffix.lower() in FORBIDDEN_SUFFIXES
    )


def inspect(path: str, data: bytes) -> list[str]:
    findings = []
    if sensitive_path(path):
        findings.append("sensitive path")
    if path == SELF or b"\0" in data:
        return findings
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            findings.append(label)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="also inspect every reachable Git blob")
    args = parser.parse_args()
    hits: set[tuple[str, str]] = set()
    for path in tracked_files():
        data = (ROOT / path).read_bytes()
        hits.update((label, path) for label in inspect(path, data))
    if args.history:
        for oid, path in history_blobs():
            data = run("git", "cat-file", "blob", oid)
            hits.update(("history: " + label, path) for label in inspect(path, data))
    if hits:
        print(f"Secret scan failed with {len(hits)} finding(s):")
        for label, path in sorted(hits):
            print(f"- {label}: {path}")
        return 1
    print("Secret scan passed: no likely credentials found in tracked content" + (" or Git history." if args.history else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
