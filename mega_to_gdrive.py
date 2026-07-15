#!/usr/bin/env python3
"""MEGA to Google Drive transfer — 5 min ON, 1 min OFF cycle.

Secrets required in repo settings:
  MEGA_LINKS       — newline-separated MEGA file links (mega.nz/file/...)
  RCLONE_CONF      — contents of rclone.conf (with a remote named 'gdrive')
  GDRIVE_REMOTE    — (optional) rclone remote name, default 'gdrive'
  GDRIVE_FOLDER    — (optional) Drive destination folder, default 'MEGA_Transfer'

Behaviour:
  The script runs in a continuous loop:
    1. Download files for 5 minutes
    2. Pause for 1 minute (MEGA quota cooldown)
    3. Repeat until all files are done
  State is persisted in mega_transfer_state.json so progress is never lost.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import hashlib
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF = os.environ.get("RCLONE_CONF", "")
GDRIVE_REMOTE = os.environ.get("GDRIVE_REMOTE", "gdrive")
GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "MEGA_Transfer")

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
STATE_FILE = os.path.join(WORKSPACE, "mega_transfer_state.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")

ACTIVE_SECONDS = 300   # 5 minutes: download window
PAUSE_SECONDS = 60     # 1 minute: cooldown pause
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
        from mega import Mega
        m = Mega()
        info = m.get_public_url_info(url)
        return info["name"], info["size"]
    except Exception as e:
        print(f"  WARN: could not fetch metadata ({e})")
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


def fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    links = [l.strip() for l in MEGA_LINKS_RAW.splitlines() if l.strip()]
    valid_links = [l for l in links if "mega.nz/file/" in l]

    if not valid_links:
        print("ERROR: No valid MEGA links found. Set MEGA_LINKS secret.")
        sys.exit(1)

    print(f"=== MEGA -> Google Drive Transfer ===", flush=True)
    print(f"Links queued: {len(valid_links)}", flush=True)
    print(f"Cycle: {ACTIVE_SECONDS//60} min ON / {PAUSE_SECONDS//60} min OFF", flush=True)
    print(f"Drive remote: {GDRIVE_REMOTE}:{GDRIVE_FOLDER}", flush=True)
    print(flush=True)

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

    # --- Pre-filter: skip already done links ---
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
        print("All files already transferred! Nothing to do.")
        return

    # --- Continuous cycle loop ---
    cycle = 0
    global_stopped = False

    while pending_links and not global_stopped:
        cycle += 1
        cycle_start = datetime.now()
        cycle_end = cycle_start + timedelta(seconds=ACTIVE_SECONDS)
        print(f"\n{'='*50}", flush=True)
        print(f"CYCLE {cycle} — active window: {ACTIVE_SECONDS//60} min", flush=True)
        print(f"  Started: {cycle_start.strftime('%H:%M:%S')}", flush=True)
        print(f"  Will pause at: {cycle_end.strftime('%H:%M:%S')}", flush=True)
        print(f"  Remaining: {len(pending_links)} files", flush=True)
        print(f"{'='*50}\n", flush=True)

        links_this_cycle = 0

        for url in list(pending_links):
            # Check time
            now = datetime.now()
            if now >= cycle_end:
                print(f"\n--- 5 min window over, pausing 1 min... ({now.strftime('%H:%M:%S')}) ---", flush=True)
                break

            key = link_id(url)

            # Metadata check
            fname, size = get_remote_metadata(url)
            if fname and drive_files.get(fname) == size:
                print(f"[{key}] '{fname}' — already in Drive, skipping.", flush=True)
                state[key] = {"filename": fname, "size": size, "status": "completed"}
                save_state(state)
                drive_files[fname] = size
                pending_links.remove(url)
                links_this_cycle += 1
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
                    links_this_cycle += 1
                    pending_links.remove(url)
                    break
                except RuntimeError as e:
                    msg = str(e)
                    if is_quota_error(msg):
                        print(f"\n!!! MEGA QUOTA EXCEEDED at {datetime.now().strftime('%H:%M:%S')} !!!", flush=True)
                        print("    Stopping all cycles. Will resume on next workflow run.", flush=True)
                        global_stopped = True
                        break
                    wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    print(f"  ERROR: {msg[:200]}", flush=True)
                    if attempt < MAX_RETRIES:
                        print(f"  Retrying in {wait}s...", flush=True)
                        time.sleep(wait)

            if global_stopped:
                break

            clear_temp_dir()

        # --- Cycle done ---
        elapsed = (datetime.now() - cycle_start).total_seconds()
        print(f"\nCycle {cycle} finished: {links_this_cycle} files in {fmt_elapsed(elapsed)}", flush=True)
        save_state(state)

        if global_stopped:
            break

        if not pending_links:
            print("\n*** ALL FILES TRANSFERRED! ***", flush=True)
            break

        # --- 1 minute pause ---
        print(f"\n--- PAUSING for {PAUSE_SECONDS//60} min at {datetime.now().strftime('%H:%M:%S')} ---", flush=True)
        print(f"    {len(pending_links)} files remaining after pause.", flush=True)
        time.sleep(PAUSE_SECONDS)
        print(f"--- RESUMING at {datetime.now().strftime('%H:%M:%S')} ---\n", flush=True)

    # --- Final report ---
    completed = [k for k, v in state.items() if v.get("status") == "completed"]
    failed = [k for k, v in state.items() if v.get("status") == "failed"]

    print(f"\n{'='*50}", flush=True)
    print(f"FINAL REPORT", flush=True)
    print(f"  Total links : {len(valid_links)}", flush=True)
    print(f"  Completed   : {len(completed)}", flush=True)
    print(f"  Failed      : {len(failed)}", flush=True)
    print(f"  Pending     : {len(pending_links)}", flush=True)
    if global_stopped:
        print(f"  Reason      : Quota exceeded — will resume on next trigger.", flush=True)
    elif not pending_links:
        print(f"  Status      : ALL DONE!", flush=True)
    print(f"{'='*50}", flush=True)

    if global_stopped or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
