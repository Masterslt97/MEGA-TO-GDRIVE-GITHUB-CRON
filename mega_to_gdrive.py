#!/usr/bin/env python3
"""MEGA to Google Drive transfer. 7 min run → 1 min off → repeat."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import hashlib
from datetime import datetime, timedelta

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF = os.environ.get("RCLONE_CONF", "")
GDRIVE_REMOTE = os.environ.get("GDRIVE_REMOTE", "") or "gdrive"
GDRIVE_FOLDER = os.environ.get("GDRIVE_FOLDER", "") or "MEGA_Transfer"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
STATE_FILE = os.path.join(WORKSPACE, "mega_transfer_state.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3
RUN_SECONDS = 420  # 7 minutes
QUOTA_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]

stats = {"downloaded": 0, "skipped": 0, "failed": 0}
total_bytes_downloaded = 0
speed_start_time = time.time()
speed_start_bytes = 0


def link_id(url):
    m = re.search(r"/file/([^#]+)", url)
    return m.group(1)[:8] if m else hashlib.md5(url.encode()).hexdigest()[:8]


def is_quota(text):
    return any(m in text.lower() for m in QUOTA_MARKERS)


def fmt_size(b):
    if b is None:
        return "unknown"
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    elif b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def get_speed():
    elapsed = time.time() - speed_start_time
    if elapsed < 1:
        return "0 B/s"
    speed = (total_bytes_downloaded - speed_start_bytes) / elapsed
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    elif speed >= 1024:
        return f"{speed / 1024:.1f} KB/s"
    return f"{speed:.0f} B/s"


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
    """List files in GDrive folder via API. Returns {filename: size}."""
    return {}  # Disabled — use state file instead (avoids 403 rate limit)


def download(url):
    global total_bytes_downloaded
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    r = subprocess.run(["megadl", "--path", TEMP_DIR, url], capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip() or f"megadl exit {r.returncode}")
    files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not files:
        raise RuntimeError("No file downloaded")
    fpath = os.path.join(TEMP_DIR, files[0])
    total_bytes_downloaded += os.path.getsize(fpath)
    return fpath


def get_gdrive_token():
    """Get fresh access token using refresh_token from rclone config."""
    import json as _json
    import urllib.request, urllib.parse

    # Get token data from rclone config
    r = subprocess.run(["rclone", "config", "show", GDRIVE_REMOTE],
                        capture_output=True, text=True, timeout=10)
    print(f"  📋 rclone config output:", flush=True)
    for line in r.stdout.strip().splitlines():
        # Mask token but show key fields
        if "token" in line.lower():
            print(f"     [token field present]", flush=True)
        else:
            print(f"     {line}", flush=True)

    refresh_token = ""
    client_id = ""
    client_secret = ""

    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("token") and "refresh_token" in line:
            token_str = line.split("=", 1)[1].strip()
            token_data = _json.loads(token_str)
            refresh_token = token_data.get("refresh_token", "")
        elif line.startswith("client_id"):
            client_id = line.split("=", 1)[1].strip()
        elif line.startswith("client_secret"):
            client_secret = line.split("=", 1)[1].strip()

    if not refresh_token:
        raise RuntimeError("No refresh_token found in rclone config")

    # Use rclone's built-in client_id if none specified
    if not client_id:
        client_id = "202264815644.apps.googleusercontent.com"
        client_secret = "X4Z3ca8xfWDb1Voo-F9a7ZxJ"

    # Refresh the token
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode()

    print(f"  🔑 Refreshing token with client_id: {client_id[:20]}...", flush=True)
    try:
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        resp = urllib.request.urlopen(req, timeout=30)
        result = _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Token refresh HTTP {e.code}: {body[:300]}")

    access_token = result.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token refresh failed: {result}")

    print(f"  ✅ Token refreshed successfully", flush=True)
    return access_token


def get_or_create_folder(parent_id, folder_name, token):
    """Find or create a folder in Google Drive. Returns folder ID."""
    import json as _json
    import urllib.request, urllib.parse

    for attempt in range(3):
        try:
            # Search for folder
            query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            url = f"https://www.googleapis.com/drive/v3/files?q={urllib.parse.quote(query)}&fields=files(id,name)"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            resp = urllib.request.urlopen(req, timeout=30)
            data = _json.loads(resp.read())

            if data.get("files"):
                return data["files"][0]["id"]

            # Create folder
            body = _json.dumps({
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]
            }).encode()
            req = urllib.request.Request("https://www.googleapis.com/drive/v3/files",
                                         data=body,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            return _json.loads(resp.read())["id"]

        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  ⚠️ Folder API 403 (attempt {attempt+1}/3), waiting {wait}s...", flush=True)
                time.sleep(wait)
                token = get_gdrive_token()
                continue
            raise


def upload(local):
    import json as _json
    import urllib.request

    fname = os.path.basename(local)
    local_size = os.path.getsize(local)

    # Get token
    token = get_gdrive_token()
    if not token:
        raise RuntimeError("Could not get Google Drive access token from rclone config")

    # Get/create folder
    folder_id = get_or_create_folder("root", GDRIVE_FOLDER, token)

    # Upload file via resumable upload with retry
    for upload_attempt in range(1, 4):
        try:
            metadata = _json.dumps({"name": fname, "parents": [folder_id]}).encode()

            # Initiate resumable upload
            req = urllib.request.Request(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
                data=metadata,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(local_size)
                }
            )
            resp = urllib.request.urlopen(req, timeout=30)
            upload_url = resp.headers.get("Location")

            if not upload_url:
                raise RuntimeError("Failed to initiate resumable upload")

            # Upload content
            with open(local, "rb") as f:
                data = f.read()
            req = urllib.request.Request(upload_url, data=data, method="PUT",
                                         headers={"Content-Length": str(local_size),
                                                  "Content-Type": "video/mp4"})
            resp = urllib.request.urlopen(req, timeout=3600)
            result = _json.loads(resp.read())

            if not result.get("id"):
                raise RuntimeError(f"Upload failed: {result}")

            print(f"  ✅ Uploaded to GDrive: {fname} (ID: {result['id']})", flush=True)
            return fname

        except urllib.error.HTTPError as e:
            if e.code == 403 and upload_attempt < 3:
                wait = 10 * upload_attempt
                print(f"  ⚠️ 403 Forbidden (attempt {upload_attempt}/3), waiting {wait}s...", flush=True)
                time.sleep(wait)
                token = get_gdrive_token()
                continue
            raise RuntimeError(f"Upload HTTP {e.code}: {e.read().decode()[:200]}")

    raise RuntimeError("Upload failed after 3 attempts")


def show_status(current, total):
    bar_len = 30
    speed = get_speed()
    # Speed-based bar: 0 MB/s = empty, 50+ MB/s = full
    speed_mbps = 0
    elapsed = time.time() - speed_start_time
    if elapsed >= 1:
        speed_mbps = (total_bytes_downloaded - speed_start_bytes) / elapsed / (1024 * 1024)
    speed_pct = min(speed_mbps / 50.0, 1.0)  # 50 MB/s = 100%
    filled = int(bar_len * speed_pct)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"  🟢 Done: {stats['downloaded']}  🟡 Skip: {stats['skipped']}  🔴 Fail: {stats['failed']}", flush=True)
    print(f"  [{bar}] ⚡ {speed}  ({current}/{total})", flush=True)


def main():
    global total_bytes_downloaded, speed_start_time, speed_start_bytes

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

    pending = []
    for url in valid:
        key = link_id(url)
        rec = state.get(key)
        if rec and rec.get("status") == "completed":
            stats["skipped"] += 1
            continue
        pending.append(url)

    total = len(valid)
    done_so_far = stats["skipped"]
    run_end = datetime.now() + timedelta(seconds=RUN_SECONDS)

    print(f"{'═' * 50}", flush=True)
    print(f"  🟢 MEGA -> Google Drive Transfer", flush=True)
    print(f"  Total: {total} | Pending: {len(pending)}", flush=True)
    print(f"  ⏱️  Run for {RUN_SECONDS//60} min, then pause 1 min", flush=True)
    print(f"  🏁 Stop at: {run_end.strftime('%H:%M:%S')}", flush=True)
    print(f"{'═' * 50}\n", flush=True)

    if done_so_far > 0:
        show_status(done_so_far, total)
        print(flush=True)

    if not pending:
        print(f"\n🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        print(f"🟢   HURRY 🎉 {total} FILES TRANSFERRED     🟢", flush=True)
        print(f"🟢   TO GDRIVE! WAh 🎉                  🟢", flush=True)
        print(f"🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        return

    speed_start_time = time.time()
    speed_start_bytes = 0

    for i, url in enumerate(pending, 1):
        # Time check
        if datetime.now() >= run_end:
            print(f"\n⏱️  7 min window over. Exiting. Cron resumes in 1 min.", flush=True)
            show_status(done_so_far, total)
            sys.exit(0)

        key = link_id(url)
        fname, size = get_metadata(url)

        # Skip if already in Drive
        if fname and drive.get(fname) == size:
            state[key] = {"filename": fname, "size": size, "status": "completed"}
            save_state(state)
            stats["skipped"] += 1
            done_so_far += 1
            print(f"🟡 [{key}] '{fname}' — skip", flush=True)
            show_status(done_so_far, total)
            print(flush=True)
            continue

        # Download + Upload
        display_name = fname or "unknown"
        print(f"⬇️  [{key}] downloading '{display_name}' ({fmt_size(size)})... [{i}/{len(pending)}]", flush=True)
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"  📥 Downloading...", flush=True)
                local = download(url)
                sz = os.path.getsize(local)
                print(f"  📤 Uploading to GDrive...", flush=True)
                name = upload(local)
                state[key] = {"filename": name, "size": sz, "status": "completed"}
                save_state(state)
                stats["downloaded"] += 1
                done_so_far += 1
                print(f"  ✅ [{key}] '{name}' ({fmt_size(sz)}) — DONE", flush=True)
                show_status(done_so_far, total)
                print(flush=True)
                success = True
                break
            except RuntimeError as e:
                msg = str(e)
                if is_quota(msg):
                    print(f"\n🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴", flush=True)
                    print(f"🔴                                      🔴", flush=True)
                    print(f"🔴   QUOTA OVER 🚫 BANDWIDTH LIMIT      🔴", flush=True)
                    print(f"🔴   Wait 1 min — Cron auto-resumes!    🔴", flush=True)
                    print(f"🔴                                      🔴", flush=True)
                    print(f"🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴", flush=True)
                    show_status(done_so_far, total)
                    sys.exit(0)
                print(f"  🔴 Attempt {attempt}/{MAX_RETRIES}: {msg[:150]}", flush=True)
                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)

        if not success:
            stats["failed"] += 1
            state[key] = {"filename": None, "size": None, "status": "failed"}
            save_state(state)
            print(f"  ❌ [{key}] FAILED after {MAX_RETRIES} attempts", flush=True)
            show_status(done_so_far, total)
            print(flush=True)

        if os.path.isdir(TEMP_DIR):
            shutil.rmtree(TEMP_DIR)

    # Final
    completed = sum(1 for v in load_state().values() if v.get("status") == "completed")
    print(f"\n{'═' * 50}", flush=True)
    print(f"  📊 RUN REPORT", flush=True)
    print(f"  🟢 Downloaded : {stats['downloaded']}", flush=True)
    print(f"  🟡 Skipped    : {stats['skipped']}", flush=True)
    print(f"  🔴 Failed     : {stats['failed']}", flush=True)
    print(f"  ⚡ Total data : {fmt_size(total_bytes_downloaded)}", flush=True)
    print(f"  📁 Total done : {completed}/{total}", flush=True)
    print(f"{'═' * 50}", flush=True)

    if completed >= total:
        print(f"\n🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)
        print(f"🟢   HURRY 🎉 {total} FILES TRANSFERRED     🟢", flush=True)
        print(f"🟢   TO GDRIVE! WAh 🎉                  🟢", flush=True)
        print(f"🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢", flush=True)


if __name__ == "__main__":
    main()
