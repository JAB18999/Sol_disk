#!/usr/bin/env python3
"""Upload every CSV file under DATA/ to the given FTP server.

Uploads are atomic (STOR a temporary name, then RENAME) so a partial transfer
never clobbers the previous good copy. Files are transferred in binary mode.
The target FTP credentials can be set on the command line or via env vars.

Examples:
    python3 upload_data_to_ftp.py
    python3 upload_data_to_ftp.py --remote-dir /sol_disk
    python3 upload_data_to_ftp.py --host 119.91.226.180 --port 21 \\
        --user test --password test

Environment variables (lower priority than CLI flags):
    FTP_HOST, FTP_PORT, FTP_USER, FTP_PASSWORD, FTP_REMOTE_DIR
"""

from __future__ import annotations

import argparse
import ftplib
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "DATA"

DEFAULT_HOST = "119.91.226.180"
DEFAULT_PORT = 21
DEFAULT_USER = "test"
DEFAULT_PASSWORD = "test"

CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload DATA/*.csv to an FTP server.")
    parser.add_argument("--host", default=os.environ.get("FTP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FTP_PORT", DEFAULT_PORT)))
    parser.add_argument("--user", default=os.environ.get("FTP_USER", DEFAULT_USER))
    parser.add_argument("--password", default=os.environ.get("FTP_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument(
        "--remote-dir",
        default=os.environ.get("FTP_REMOTE_DIR", "."),
        help="Remote directory to upload into (created if missing). Default: current FTP dir.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Local directory whose CSV files are uploaded. Default: ./DATA",
    )
    parser.add_argument("--active", action="store_true", help="Use active mode instead of passive mode.")
    return parser.parse_args()


def ensure_remote_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    """Create the remote directory (and parents) if it does not exist."""
    if remote_dir in (".", "/", ""):
        return
    parts = [p for p in remote_dir.split("/") if p]
    current = "/"
    for part in parts:
        target = current + part
        try:
            ftp.mkd(target)
        except ftplib.error_perm as exc:
            code = str(exc).split(None, 1)[0] if str(exc).strip() else ""
            if code not in ("521", "550"):  # 550 = already exists, 521 = dir exists (some servers)
                raise
        current = target + "/"


def upload_one(ftp: ftplib.FTP, local_path: Path) -> str:
    """Atomically upload one file; returns the final remote filename."""
    if local_path.stat().st_size == 0:
        raise RuntimeError(f"{local_path.name} is empty, skipping")
    # Binary smoke check (no encoding assumptions, per past CSV pitfall).
    with local_path.open("rb") as probe:
        if not probe.read(1):
            raise RuntimeError(f"{local_path.name} produced no bytes")

    remote_name = local_path.name
    temp_name = f".{remote_name}.part"
    with local_path.open("rb") as source:
        ftp.storbinary(f"STOR {temp_name}", source)
    try:
        try:
            ftp.delete(remote_name)
        except ftplib.error_perm:
            pass  # may not exist yet
        ftp.rename(temp_name, remote_name)
    except ftplib.Error:
        try:
            ftp.delete(temp_name)
        except ftplib.error_perm:
            pass
        raise
    return remote_name


def connect(args: argparse.Namespace) -> ftplib.FTP:
    ftp = ftplib.FTP(encoding="utf-8")
    ftp.connect(args.host, args.port, timeout=CONNECT_TIMEOUT_SECONDS)
    ftp.login(args.user, args.password)
    ftp.set_pasv(not args.active)
    ftp.set_debuglevel(0)
    # Increase the control-socket read timeout so large files don't stall.
    ftp.sock.settimeout(READ_TIMEOUT_SECONDS)
    return ftp


def main() -> int:
    args = parse_args()

    csv_files = sorted(args.data_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {args.data_dir}", flush=True)
        return 1

    print(f"Host: {args.host}:{args.port} | user: {args.user} | remote dir: {args.remote_dir}", flush=True)
    print(f"Files to upload ({len(csv_files)}):", flush=True)
    for f in csv_files:
        print(f"  - {f.name} ({f.stat().st_size:,} bytes)", flush=True)
    print(flush=True)

    ftp: ftplib.FTP | None = None
    try:
        ftp = connect(args)
        ensure_remote_dir(ftp, args.remote_dir)
        if args.remote_dir not in (".", "/", ""):
            ftp.cwd(args.remote_dir)
        print(f"Remote cwd: {ftp.pwd()}", flush=True)
        print(flush=True)

        successes: list[str] = []
        failures: list[tuple[str, str]] = []

        for local_path in csv_files:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    # Re-login fresh each file to avoid control-connection stalls.
                    if ftp is not None:
                        try:
                            ftp.quit()
                        except Exception:
                            pass
                    ftp = connect(args)
                    if args.remote_dir not in (".", "/", ""):
                        ftp.cwd(args.remote_dir)
                    remote_name = upload_one(ftp, local_path)
                    successes.append(local_path.name)
                    print(f"[OK]  {local_path.name} -> {remote_name}", flush=True)
                    break
                except (ftplib.Error, OSError, socket.error) as exc:
                    print(
                        f"[RETRY {attempt}/{MAX_RETRIES}] {local_path.name}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    if attempt == MAX_RETRIES:
                        failures.append((local_path.name, str(exc)))
                    else:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        print(flush=True)
        print(f"Success: {len(successes)}/{len(csv_files)}", flush=True)
        if failures:
            print("Failures:", flush=True)
            for name, err in failures:
                print(f"  - {name}: {err}", flush=True)
            return 1
        return 0
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
