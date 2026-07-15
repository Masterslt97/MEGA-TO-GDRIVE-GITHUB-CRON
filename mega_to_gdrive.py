#!/usr/bin/env python3
"""MEGA to Google Drive transfer — auto-resume on quota hit.

Secrets required:
  MEGA_LINKS       — newline-separated MEGA file links
  RCLONE_CONF      — contents of rclone.conf
  GDRIVE_REMOTE    — (optional) rclone remote name, default 'gdrive'
  GDRIVE_FOLDER    — (optional) Drive folder, default 'MEGA_Transfer'

Logic:
  Download files one by one.
  Quota hit → exit immediately.
  Cron triggers again → script resumes from state file.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import hashlib

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF = os.environ.get("RCLONE_CONF", "")
GDRIVE_REMOTE = os.environ.get("GDRIVE_REMOTE", "") or "gdrive"
GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "") or "MEGA_Transfer"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
STATE_FILE = os.path.join(WORKSPACE, "mega_transfer_state.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 5
QUOTA_ERROR_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def link_id(url: str) -> str:
    m = re.search(r"/file/([^#]+)", url)
    return m.group(1)[:8] if m else hashlib.md5(url.encode()).hexdigest()[:8]


def is_quota_error(text: str) -> bool:
    return any(marker in text.lower() for marker in QUOTA_ERROR_MARKERS)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_remote_metadata(url: str):
    try:
        result = subprocess.run(
            ["megadl", "--info", url],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout.strip()
        name_match = re.search(r"File:\s+(.+?)\s+\(", output)
        size_match = re.search(r"\((\d+)\s*bytes?\)", output)
        if name_match and size_match:
            return name_match.group(1), int(size_match.group(1))
    except Exception:
        pass
    return None, None


def scan_drive_folder() -> dict:
    existing = {}
    result = subprocess.run(
        ["rclone", "ls", f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}", "--max-depth", "1"],
        capture_output=True, text=True, timeout=120
    )
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                existing[parts[1]] = int(parts[0])
            except ValueError:
                pass
    return existing


def clear_temp_dir():
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)


def download_one(url: str) -> str:
    clear_temp_dir()
    cmd = ["megadl", "--path", TEMP_DIR, url]
    print(f"  Running: {' '.join(cmd)}", flush=True)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(output or f"megadl exited with code {result.returncode}")

    downloaded_files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not downloaded_files:
        raise RuntimeError(f"megadl exited 0 but no file in temp dir. Output: {result.stdout[:300]}")
    return os.path.join(TEMP_DIR, downloaded_files[0])


def upload_to_drive(local_path: str) -> str:
    remote_path = f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}/"
    cmd = ["rclone", "copy", local_path, remote_path, "--progress"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"rclone copy failed: {(result.stdout + result.stderr)[:500]}")
    return os.path.basename(local_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    links = [l.strip() for l in MEGA_LINKS_RAW.splitlines() if l.strip()]
    valid_links = [l for l in links if "mega.nz/file/" in l]

    if not valid_links:
        print("ERROR: No valid MEGA links found.")
        sys.exit(1)

    print(f"=== MEGA -> Google Drive Transfer ===", flush=True)
    print(f"Links queued: {len(valid_links)}", flush=True)
    print(f"Drive remote: {GDRIVE_REMOTE}:{GDRIVE_FOLDER}", flush=True)

    # --- Setup rclone conf ---
    conf_dir = os.path.expanduser("~/.config/rclone")
    os.makedirs(conf_dir, exist_ok=True)
    conf_path = os.path.join(conf_dir, "rclone.conf")
    if not os.path.exists(conf_path):
        if not RCLONE_CONF:
            print("ERROR: RCLONE_CONF secret is empty.")
            sys.exit(1)
        with open(conf_path, "w") as f:
            f.write(RCLONE_CONF)
        print("rclone.conf written from secret", flush=True)

    os.makedirs(TEMP_DIR, exist_ok=True)

    # --- State + remote scan ---
    state = load_state()
    drive_files = scan_drive_folder()
    print(f"state.json: {len(state)} tracked, Drive: {len(drive_files)} files", flush=True)

    # --- Find pending links ---
    pending_links = []
    already_done = 0
    for url in valid_links:
        key = link_id(url)
        rec = state.get(key)
        if rec and rec.get("status") == "completed":
            fname, size = rec.get("filename"), rec.get("size")
            if fname and drive_files.get(fname) == size:
                already_done += 1
                continue
        pending_links.append(url)

    print(f"Already done: {already_done}, Pending: {len(pending_links)}", flush=True)

    if not pending_links:
        print("ALL FILES DONE! Nothing to do.")
        return

    # --- Download loop ---
    quota_hit = False
    downloaded_this_run = 0

    for url in pending_links:
        key = link_id(url)

        # Metadata check
        fname, size = get_remote_metadata(url)
        if fname and drive_files.get(fname) == size:
            print(f"[{key}] '{fname}' — already in Drive, skipping.", flush=True)
            state[key] = {"filename": fname, "size": size, "status": "completed"}
            save_state(state)
            drive_files[fname] = size
            downloaded_this_run += 1
            continue

        print(f"[{key}] '{fname}' ({size} bytes) — downloading...", flush=True)

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"  attempt {attempt}/{MAX_RETRIES}...", flush=True)
                local_path = download_one(url)
                dfname = upload_to_drive(local_path)
                dsize = os.path.getsize(local_path)
                print(f"  -> uploaded '{dfname}' ({dsize} bytes)", flush=True)

                state[key] = {"filename": dfname, "size": dsize, "status": "completed"}
                save_state(state)
                drive_files[dfname] = dsize
                success = True
                downloaded_this_run += 1
                break
            except RuntimeError as e:
                msg = str(e)
                if is_quota_error(msg):
                    print(f"\n!!! QUOTA HIT at {__import__('datetime').datetime.now().strftime('%H:%M:%S')} !!!", flush=True)
                    print("    Exiting. Cron will restart in 10 min.", flush=True)
                    quota_hit = True
                    break
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(f"  ERROR: {msg[:200]}", flush=True)
                if attempt < MAX_RETRIES:
                    print(f"  Retrying in {wait}s...", flush=True)
                    time.sleep(wait)

        if quota_hit:
            break

        clear_temp_dir()

    # --- Report ---
    completed = [k for k, v in state.items() if v.get("status") == "completed"]
    remaining = len(valid_links) - len(completed)

    print(f"\n=== Run Report ===", flush=True)
    print(f"  Downloaded this run : {downloaded_this_run}", flush=True)
    print(f"  Total completed     : {len(completed)}", flush=True)
    print(f"  Remaining           : {remaining}", flush=True)
    if quota_hit:
        print(f"  Status: QUOTA HIT — cron will resume in ~10 min", flush=True)
    elif remaining == 0:
        print(f"  Status: ALL DONE!", flush=True)
    print(f"===================", flush=True)


if __name__ == "__main__":
    main()
