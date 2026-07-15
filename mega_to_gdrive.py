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

# Counters
stats = {"downloaded": 0, "skipped": 0, "failed": 0}


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


def show_progress(current, total):
    """Show clear progress with filled bar."""
    pct = (current / total * 100) if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"  🟢 Done: {stats['downloaded']}  🟡 Skip: {stats['skipped']}  🔴 Fail: {stats['failed']}", flush=True)
    print(f"  [{bar}] {pct:.1f}%  ({current}/{total})", flush=True)


def main():
    links = [l.strip() for l in MEGA_LINKS_RAW.splitlines() if l.strip()]
    valid = [l for l in links if "mega.nz/file/" in l]
    if not valid:
        print("🔴 ERROR: No valid MEGA links."); sys.exit(1)

    conf_path = os.path.expanduser("~/.config/rclone/rclone.conf")
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)
    if not os.path.exists(conf_path):
        if not RCLONE_CONF:
            print("🔴 ERROR: RCLONE_CONF empty."); sys.exit(1)
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
                stats["skipped"] += 1
                continue
        pending.append(url)

    total = len(valid)
    done_so_far = stats["skipped"]

    print(f"{'═' * 50}", flush=True)
    print(f"  🟢 MEGA -> Google Drive Transfer", flush=True)
    print(f"  Total: {total} | Skipped: {stats['skipped']} | Pending: {len(pending)}", flush=True)
    print(f"{'═' * 50}\n", flush=True)

    # Show initial progress if some are already done
    if done_so_far > 0:
        show_progress(done_so_far, total)
        print(flush=True)

    if not pending:
        print(f"\n🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        print(f"🟢                                      🟢", flush=True)
        print(f"🟢   HURRY 🎉 {total} FILES TRANSFERRED     🟢", flush=True)
        print(f"🟢   TO GDRIVE! WAh 🎉                  🟢", flush=True)
        print(f"🟢                                      🟢", flush=True)
        print(f"🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        return

    for i, url in enumerate(pending, 1):
        key = link_id(url)
        fname, size = get_metadata(url)

        # Already in Drive
        if fname and drive.get(fname) == size:
            state[key] = {"filename": fname, "size": size, "status": "completed"}
            save_state(state); drive[fname] = size
            stats["skipped"] += 1
            done_so_far += 1
            print(f"🟡 [{key}] '{fname}' — skip (already in Drive)", flush=True)
            show_progress(done_so_far, total)
            print(flush=True)
            continue

        # Download
        print(f"⬇️  [{key}] downloading '{fname}' ({size} bytes)... [{i}/{len(pending)}]", flush=True)
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                local = download(url)
                name = upload(local)
                sz = os.path.getsize(local)
                state[key] = {"filename": name, "size": sz, "status": "completed"}
                save_state(state); drive[name] = sz
                stats["downloaded"] += 1
                done_so_far += 1
                print(f"🟢 [{key}] '{name}' ({sz} bytes) — DONE", flush=True)
                show_progress(done_so_far, total)
                print(flush=True)
                success = True
                break
            except RuntimeError as e:
                msg = str(e)
                if is_quota(msg):
                    print(f"\n🔴 [{key}] QUOTA HIT — exiting. Cron resumes in 1 min.", flush=True)
                    show_progress(done_so_far, total)
                    sys.exit(0)
                print(f"🔴 [{key}] attempt {attempt}/{MAX_RETRIES}: {msg[:150]}", flush=True)
                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)

        if not success:
            stats["failed"] += 1
            state[key] = {"filename": None, "size": None, "status": "failed"}
            save_state(state)
            print(f"🔴 [{key}] FAILED after {MAX_RETRIES} attempts", flush=True)
            show_progress(done_so_far, total)
            print(flush=True)

        if os.path.isdir(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)

    # Final report
    completed = sum(1 for v in load_state().values() if v.get("status") == "completed")
    print(f"\n{'═' * 50}", flush=True)
    print(f"  📊 FINAL REPORT", flush=True)
    print(f"  🟢 Downloaded : {stats['downloaded']}", flush=True)
    print(f"  🟡 Skipped    : {stats['skipped']}", flush=True)
    print(f"  🔴 Failed     : {stats['failed']}", flush=True)
    print(f"  Total done    : {completed}/{total}", flush=True)

    # Graph
    bar_len = 40
    g = int(bar_len * stats['downloaded'] / total) if total else 0
    y = int(bar_len * stats['skipped'] / total) if total else 0
    r = bar_len - g - y
    graph = "🟢" * g + "🟡" * y + "🔴" * max(r, 0)
    print(f"\n  {graph}", flush=True)
    print(f"  🟢=Downloaded 🟡=Skipped 🔴=Failed", flush=True)
    print(f"{'═' * 50}", flush=True)

    if completed >= total:
        print(f"\n🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        print(f"🟢                                      🟢", flush=True)
        print(f"🟢   HURRY 🎉 {total} FILES TRANSFERRED     🟢", flush=True)
        print(f"🟢   TO GDRIVE! WAh 🎉                  🟢", flush=True)
        print(f"🟢                                      🟢", flush=True)
        print(f"🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)


if __name__ == "__main__":
    main()
