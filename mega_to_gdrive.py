#!/usr/bin/env python3
"""MEGA to Google Drive transfer. Quota hit pe exit, cron 1 min baad resume."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import hashlib

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF = os.environ.get("RCLONE_CONF", "")
GDRIVE_REMOTE = os.environ.get("GDRIVE_REMOTE", "") or "gdrive"
GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "") or "MEGA_Transfer"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
STATE_FILE = os.path.join(WORKSPACE, "mega_transfer_state.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3
QUOTA_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]


def link_id(url):
    m = re.search(r"/file/([^#]+)", url)
    return m.group(1)[:8] if m else hashlib.md5(url.encode()).hexdigest()[:8]


def is_quota(text):
    return any(m in text.lower() for m in QUOTA_MARKERS)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def get_metadata(url):
    try:
        r = subprocess.run(["megadl", "--info", url], capture_output=True, text=True, timeout=30)
        out = r.stdout.strip()
        name = re.search(r"File:\s+(.+?)\s+\(", out)
        size = re.search(r"\((\d+)\s*bytes?\)", out)
        if name and size:
            return name.group(1), int(size.group(1))
    except Exception:
        pass
    return None, None


def scan_drive():
    existing = {}
    r = subprocess.run(["rclone", "ls", f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}", "--max-depth", "1"],
                        capture_output=True, text=True, timeout=120)
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                existing[parts[1]] = int(parts[0])
            except ValueError:
                pass
    return existing


def download(url):
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    r = subprocess.run(["megadl", "--path", TEMP_DIR, url], capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip() or f"megadl exit {r.returncode}")
    files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not files:
        raise RuntimeError("No file downloaded")
    return os.path.join(TEMP_DIR, files[0])


def upload(local):
    r = subprocess.run(["rclone", "copy", local, f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}/", "--progress"],
                        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"rclone failed: {(r.stdout + r.stderr)[:300]}")
    return os.path.basename(local)


def main():
    links = [l.strip() for l in MEGA_LINKS_RAW.splitlines() if l.strip()]
    valid = [l for l in links if "mega.nz/file/" in l]
    if not valid:
        print("ERROR: No valid MEGA links."); sys.exit(1)

    conf_path = os.path.expanduser("~/.config/rclone/rclone.conf")
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)
    if not os.path.exists(conf_path):
        if not RCLONE_CONF:
            print("ERROR: RCLONE_CONF empty."); sys.exit(1)
        open(conf_path, "w").write(RCLONE_CONF)

    os.makedirs(TEMP_DIR, exist_ok=True)
    state = load_state()
    drive = scan_drive()

    pending = []
    for url in valid:
        key = link_id(url)
        rec = state.get(key)
        if rec and rec.get("status") == "completed":
            f, s = rec.get("filename"), rec.get("size")
            if f and drive.get(f) == s:
                continue
        pending.append(url)

    done = len(valid) - len(pending)
    print(f"Done: {done} | Pending: {len(pending)}", flush=True)
    if not pending:
        print("ALL DONE!"); return

    for url in pending:
        key = link_id(url)
        fname, size = get_metadata(url)
        if fname and drive.get(fname) == size:
            state[key] = {"filename": fname, "size": size, "status": "completed"}
            save_state(state); drive[fname] = size
            print(f"[{key}] skip (in Drive)", flush=True)
            continue

        print(f"[{key}] downloading...", flush=True)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                local = download(url)
                name = upload(local)
                sz = os.path.getsize(local)
                state[key] = {"filename": name, "size": sz, "status": "completed"}
                save_state(state); drive[name] = sz
                print(f"  -> '{name}' ({sz}) done", flush=True)
                break
            except RuntimeError as e:
                msg = str(e)
                if is_quota(msg):
                    print(f"\n!!! QUOTA HIT — exiting. Cron resumes in 1 min. !!!", flush=True)
                    sys.exit(0)
                print(f"  ERROR: {msg[:200]}", flush=True)
                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)

        if os.path.isdir(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)

    completed = sum(1 for v in load_state().values() if v.get("status") == "completed")
    print(f"\n=== {completed}/{len(valid)} completed ===", flush=True)


if __name__ == "__main__":
    main()
