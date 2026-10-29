#!/usr/bin/env python3
"""Upload every CSV file under DATA/ to the given FTP server.

The upload is performed in three phases so the remote directory is never in a
partially-updated state:

1. Upload every local CSV to a temporary `.part` file on the FTP server.
2. Delete the old/stale CSV and `.part` files on the server.
3. Rename all temporary `.part` files to their final names.

A fourth opportunistic safeguard was added to avoid unnecessary re-uploads: when
a remote file already exists and its SHA-256 digest matches the local file, we
skip the transfer entirely instead of rewriting the same CSV again.

If any step fails before the final rename, the old remote files are left
untouched and the temporary files are cleaned up, so a failed run never leaves
bad data behind. Files are transferred in binary mode. Credentials can be set
on the command line or via env vars.

Examples:
    python3 upload_data_to_ftp.py
    python3 upload_data_to_ftp.py --remote-dir /sol_disk
    python3 upload_data_to_ftp.py --host 119.91.226.180 --port 21 --user test --password test

Environment variables (lower priority than CLI flags):
    FTP_HOST, FTP_PORT, FTP_USER, FTP_PASSWORD, FTP_REMOTE_DIR
"""

from __future__ import annotations

import argparse
import ftplib
import hashlib
import io
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "DATA"

DEFAULT_HOST = "119.91.226.180"
DEFAULT_PORT = 2121
DEFAULT_USER = "ftpuser"
DEFAULT_PASSWORD = "1234567890987654321"

CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3.0


def env_or(env_key: str, default: str) -> str:
    """Return the env var value, falling back to default when unset or empty."""
    value = os.environ.get(env_key)
    return value if value else default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload DATA/*.csv to an FTP server.")
    parser.add_argument("--host", default=env_or("FTP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(env_or("FTP_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--user", default=env_or("FTP_USER", DEFAULT_USER))
    parser.add_argument("--password", default=env_or("FTP_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument(
        "--remote-dir",
        default=env_or("FTP_REMOTE_DIR", "."),
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


def list_remote_data_files(ftp: ftplib.FTP) -> list[str]:
    """Return the CSV / leftover .part file names in the current remote directory."""
    names: list[str] = []
    try:
        for name, facts in ftp.mlsd():
            if facts.get("type") in ("dir", "cdir", "pdir"):
                continue
            names.append(name)
    except (ftplib.error_perm, ftplib.error_proto):
        # Server without MLSD support: fall back to a plain name list.
        for path in ftp.nlst():
            names.append(path.rsplit("/", 1)[-1])
    return [
        name
        for name in names
        if name.lower().endswith(".csv") or name.lower().endswith(".part")
    ]


def temp_name_for(remote_name: str) -> str:
    return f".{remote_name}.part"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def download_remote_file(ftp: ftplib.FTP, remote_name: str) -> bytes:
    """Return the full contents of a remote FTP file as bytes."""
    buffer = io.BytesIO()

    def _write(chunk: bytes) -> int:
        buffer.write(chunk)
        return len(chunk)

    ftp.retrbinary(f"RETR {remote_name}", _write)
    return buffer.getvalue()


def remote_file_matches(local_path: Path, ftp: ftplib.FTP, remote_name: str) -> bool:
    """Return True when the local file has identical contents to the remote file."""
    try:
        remote_size = ftp.size(remote_name)
    except (ftplib.Error, OSError):
        return False

    try:
        local_size = local_path.stat().st_size
    except OSError:
        return False
    if local_size != remote_size:
        return False

    try:
        with local_path.open("rb") as source:
            local_bytes = source.read()
        remote_bytes = download_remote_file(ftp, remote_name)
    except (OSError, ftplib.Error):
        return False

    return sha256_bytes(local_bytes) == sha256_bytes(remote_bytes)


def upload_temp(ftp: ftplib.FTP, local_path: Path) -> str:
    """Upload a local file to a temporary `.part` name on the remote server."""
    if local_path.stat().st_size == 0:
        raise RuntimeError(f"{local_path.name} is empty, skipping")
    with local_path.open("rb") as probe:
        if not probe.read(1):
            raise RuntimeError(f"{local_path.name} produced no bytes")

    temp = temp_name_for(local_path.name)
    with local_path.open("rb") as source:
        ftp.storbinary(f"STOR {temp}", source)
    return temp


def rename_temp(ftp: ftplib.FTP, temp_name: str, final_name: str) -> None:
    ftp.rename(temp_name, final_name)


def delete_remote_files(ftp: ftplib.FTP, names: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Delete the given remote files; return (deleted, failures)."""
    deleted: list[str] = []
    failures: list[tuple[str, str]] = []
    for name in names:
        try:
            ftp.delete(name)
            deleted.append(name)
            print(f"[DEL] {name}", flush=True)
        except (ftplib.Error, OSError) as exc:
            failures.append((name, str(exc)))
            print(f"[WARN] could not delete {name}: {type(exc).__name__}: {exc}", flush=True)
    return deleted, failures


def connect(args: argparse.Namespace) -> ftplib.FTP:
    ftp = ftplib.FTP(encoding="utf-8")
    ftp.connect(args.host, args.port, timeout=CONNECT_TIMEOUT_SECONDS)
    ftp.login(args.user, args.password)
    ftp.set_pasv(not args.active)
    ftp.set_debuglevel(0)
    ftp.sock.settimeout(READ_TIMEOUT_SECONDS)
    return ftp


def ensure_cwd(ftp: ftplib.FTP, args: argparse.Namespace) -> None:
    ensure_remote_dir(ftp, args.remote_dir)
    if args.remote_dir not in (".", "/", ""):
        ftp.cwd(args.remote_dir)


def cleanup_temps(ftp: ftplib.FTP, temp_names: list[str]) -> None:
    """Best-effort removal of temporary files after a failure."""
    for name in temp_names:
        try:
            ftp.delete(name)
            print(f"[CLEANUP] removed temp {name}", flush=True)
        except (ftplib.Error, OSError):
            pass


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
    pending_renames: list[tuple[str, str]] = []  # (temp_name, final_name)
    protected_final_names: set[str] = set()

    try:
        print("Phase 1: sync local files to remote, skipping unchanged content", flush=True)
        for local_path in csv_files:
            final = local_path.name
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if ftp is not None:
                        try:
                            ftp.quit()
                        except Exception:
                            pass
                    ftp = connect(args)
                    ensure_cwd(ftp, args)
                    remote_names = set(list_remote_data_files(ftp))
                    if final in remote_names and remote_file_matches(local_path, ftp, final):
                        protected_final_names.add(final)
                        print(f"[SKIP] {final}: identical remote content, no upload needed", flush=True)
                        break
                    temp = upload_temp(ftp, local_path)
                    pending_renames.append((temp, final))
                    print(f"[TEMP] {local_path.name} -> {temp}", flush=True)
                    break
                except (ftplib.Error, OSError, socket.error) as exc:
                    print(
                        f"[RETRY {attempt}/{MAX_RETRIES}] {local_path.name}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    if attempt == MAX_RETRIES:
                        print(f"[FAIL] {local_path.name}: aborting, cleaning up temps", flush=True)
                        if ftp is not None:
                            try:
                                ensure_cwd(ftp, args)
                                cleanup_temps(ftp, [t for t, _ in pending_renames])
                            except Exception:
                                pass
                        print(flush=True)
                        print(f"Cleaned up temps: {len(pending_renames)}", flush=True)
                        print(f"Failed before any rename: old remote files remain untouched", flush=True)
                        return 1
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        print(flush=True)

        protected_temps = {temp for temp, _ in pending_renames}
        stale_names: list[str] = []
        try:
            stale_names = sorted(
                name
                for name in list_remote_data_files(ftp)  # type: ignore[arg-type]
                if name not in protected_temps and name not in protected_final_names
            )
        except (ftplib.Error, OSError) as exc:
            print(f"[FAIL] could not list remote directory for cleanup: {type(exc).__name__}: {exc}", flush=True)
            cleanup_temps(ftp, [t for t, _ in pending_renames])  # type: ignore[arg-type]
            return 1

        if stale_names:
            print(f"Phase 2: delete {len(stale_names)} old/stale file(s)", flush=True)
            deleted, delete_failures = delete_remote_files(ftp, stale_names)  # type: ignore[arg-type]
            if delete_failures:
                print(f"[FAIL] {len(delete_failures)} file(s) could not be deleted; aborting rename", flush=True)
                cleanup_temps(ftp, [t for t, _ in pending_renames])  # type: ignore[arg-type]
                print(flush=True)
                print("Failures during cleanup:", flush=True)
                for name, err in delete_failures:
                    print(f"  - {name}: {err}", flush=True)
                return 1
        else:
            deleted: list[str] = []
            print("Phase 2: no old files to remove", flush=True)

        print(flush=True)

        print(f"Phase 3: rename {len(pending_renames)} temporary file(s) to final names", flush=True)
        rename_failures: list[tuple[str, str]] = []
        for temp, final in pending_renames:
            try:
                rename_temp(ftp, temp, final)  # type: ignore[arg-type]
                print(f"[OK]  {final}", flush=True)
            except (ftplib.Error, OSError) as exc:
                rename_failures.append((temp, str(exc)))
                print(f"[WARN] could not rename {temp} -> {final}: {type(exc).__name__}: {exc}", flush=True)

        print(flush=True)
        print(f"Removed old files: {len(deleted)}", flush=True)
        print(f"Renamed new files: {len(pending_renames) - len(rename_failures)}/{len(pending_renames)}", flush=True)
        print(f"Skipped unchanged files: {len(protected_final_names)}", flush=True)

        if rename_failures:
            print("Files left as .part (manual rename required):", flush=True)
            for temp, err in rename_failures:
                print(f"  - {temp}: {err}", flush=True)
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
