#!/usr/bin/env python3
"""MEGA to Google Drive multi-folder transfer with artifact-based state tracking."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF_RAW = os.environ.get("RCLONE_CONF", "")

# mega.py uses deprecated asyncio.coroutine — restore if missing
import asyncio
if not hasattr(asyncio, "coroutine"):
    asyncio.coroutine = lambda c: c
GDRIVE_REMOTE = "gdrive"
BASE_FOLDER = "MEGA_Transfer"
QUOTA_MAX = 5 * 1024 * 1024 * 1024
QUOTA_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
COMPLETED_FILE = os.path.join(WORKSPACE, "completed_links.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3


def fmt_size(b):
    if b is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def is_quota(text):
    return any(m in text.lower() for m in QUOTA_MARKERS)


def log(msg):
    print(msg, flush=True)


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_completed():
    if os.path.exists(COMPLETED_FILE):
        try:
            with open(COMPLETED_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"folders": {}, "completed": [], "current_folder": None, "oversized": []}


def save_completed(state):
    with open(COMPLETED_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_file_info(url):
    try:
        from mega import Mega
        info = Mega().get_public_url_info(url)
        name = info.get("name")
        size = info.get("size")
        if name and size is not None:
            return name, size
    except Exception as e:
        log(f"  [debug] mega.py error: {e}")
        print(f"::error::get_file_info: {e}")
    try:
        r = subprocess.run(
            ["megadl", "--info", url],
            capture_output=True, text=True, timeout=30
        )
        out = (r.stdout + " " + r.stderr).strip()
        name = re.search(r"(?:File|Name):\s*(.+?)\s*\(", out)
        size = re.search(r"\((\d+)\s*bytes?\)", out)
        if name and size:
            return name.group(1), int(size.group(1))
    except Exception:
        pass
    return None, None


def download_file(url):
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    # Try mega.py first (more reliable), fallback to megadl
    try:
        from mega import Mega
        Mega().download_url(url, dest_path=TEMP_DIR)
        files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
        if files:
            return os.path.join(TEMP_DIR, files[0])
    except Exception as e:
        err = f"mega.py download failed: {e}"
        log(f"  [debug] {err}")
        print(f"::error::download_file: {err[:200]}")
    r = subprocess.run(
        ["megadl", "--path", TEMP_DIR, url],
        capture_output=True, text=True, timeout=3600
    )
    if r.stdout:
        for line in r.stdout.strip().splitlines():
            log(f"  {line}")
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip() or f"megadl exit {r.returncode}")
    files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not files:
        raise RuntimeError("No file downloaded")
    return os.path.join(TEMP_DIR, files[0])


def ensure_gdrive_folder(folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}"
    r = subprocess.run(
        ["rclone", "mkdir", target],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        log(f"  warning: rclone mkdir stderr: {r.stderr[:200]}")


def upload_file(filepath, folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/"
    r = subprocess.run(
        ["rclone", "copy", filepath, target],
        capture_output=True, text=True, timeout=3600
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or f"rclone copy exit {r.returncode}")
    return os.path.basename(filepath)


def verify_upload(filename, file_size, folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/{filename}"
    r = subprocess.run(
        ["rclone", "lsjson", target],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0 or not r.stdout.strip():
        return False
    try:
        files = json.loads(r.stdout)
        for f in files:
            if f.get("Name") == filename and f.get("Size") == file_size:
                return True
    except (json.JSONDecodeError, KeyError):
        pass
    return False


def main():
    # Setup rclone config
    conf_dir = os.path.expanduser("~/.config/rclone")
    conf_path = os.path.join(conf_dir, "rclone.conf")
    os.makedirs(conf_dir, exist_ok=True)
    if not os.path.exists(conf_path):
        if not RCLONE_CONF_RAW:
            log("ERROR: RCLONE_CONF secret is empty")
            sys.exit(1)
        with open(conf_path, "w") as f:
            f.write(RCLONE_CONF_RAW)
        log("  rclone.conf written")

    # Load artifact state
    state = load_completed()
    folders = state.get("folders", {})
    completed = state.get("completed", [])
    current_folder = state.get("current_folder")
    oversized = state.get("oversized", [])

    # Parse MEGA_LINKS JSON
    if not MEGA_LINKS_RAW.strip():
        log("ERROR: MEGA_LINKS secret is empty")
        sys.exit(1)

    try:
        all_links = json.loads(MEGA_LINKS_RAW)
    except json.JSONDecodeError as e:
        log(f"ERROR: MEGA_LINKS is not valid JSON: {e}")
        log("   Expected: {\"FolderName\": [\"url1\", \"url2\"]}")
        sys.exit(1)

    if not isinstance(all_links, dict):
        log("ERROR: MEGA_LINKS must be a JSON object {\"folder\": [urls]}")
        sys.exit(1)

    # Ensure all folders from secret are in state
    for folder_name, links in all_links.items():
        if folder_name not in folders:
            folders[folder_name] = {
                "total": len(links),
                "done": 0,
                "status": "pending"
            }

    # Auto-activate first pending folder
    if not current_folder or current_folder not in folders:
        for name, fdata in folders.items():
            if fdata["status"] == "pending":
                fdata["status"] = "active"
                current_folder = name
                state["current_folder"] = name
                break

    state["folders"] = folders
    save_completed(state)

    # Build lookup sets
    completed_urls = set(item["url"] for item in completed)
    oversized_urls = set(item["url"] for item in oversized)

    # Stats
    total_pending_all = sum(
        f["total"] - f["done"] for f in folders.values() if f["status"] != "completed"
    )

    log("=" * 55)
    log(f"  MEGA -> GDrive Transfer | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 55)
    log(f"  Artifact loaded: {len(completed)} completed files, {len(oversized)} oversized")
    log(f"  Total pending: {total_pending_all}")
    log("-" * 55)
    for name, fdata in folders.items():
        icon = "ACTIVE" if fdata["status"] == "active" else "DONE" if fdata["status"] == "completed" else "WAIT"
        log(f"  [{icon}] {name}: {fdata['done']}/{fdata['total']}")
    log("-" * 55)

    if total_pending_all == 0:
        log(f"\n  ALL FOLDERS COMPLETE! Sab files transfer ho gayi!")
        log("=" * 55)
        return

    # Find active folder
    active_folder = None
    for name, fdata in folders.items():
        if fdata["status"] == "active":
            active_folder = name
            break

    if not active_folder:
        log("  No active folder found. Check state.")
        sys.exit(0)

    folder_links = all_links.get(active_folder, [])
    pending = []
    for url in folder_links:
        if url in completed_urls or url in oversized_urls:
            continue
        pending.append(url)

    total = len(pending)
    log(f"\n  Active: [{active_folder}] -> {total} files pending")
    log("=" * 55 + "\n")

    if total == 0:
        folders[active_folder]["status"] = "completed"
        folders[active_folder]["done"] = folders[active_folder]["total"]
        state["folders"] = folders
        save_completed(state)
        log(f"  [{active_folder}] already complete. Moving on.")
        sys.exit(0)

    # Process files
    quota_used = 0
    processed = 0

    for idx, url in enumerate(pending, 1):
        log(f"  --- [{idx}/{total}] {active_folder} ---")
        log(f"  Fetching: {url[:60]}...")

        filename, file_size = get_file_info(url)
        metadata_ok = filename and file_size is not None

        if metadata_ok:
            log(f"  [{active_folder}] \"{filename}\" | Size: {fmt_size(file_size)}")
            if file_size > QUOTA_MAX:
                log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB")
                oversized.append({
                    "url": url, "filename": filename,
                    "size": file_size, "target_folder": active_folder
                })
                state["oversized"] = oversized
                save_completed(state)
                continue
            if quota_used + file_size > QUOTA_MAX:
                log(f"  Quota full: {fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB")
                log(f"  Skipping \"{filename}\" for this run")
                break
        else:
            log(f"  (metadata unavailable — downloading directly)")

        # Download
        dl_start = time.time()
        log(f"  DOWNLOADING: \"{filename or '?'}\" ({fmt_size(file_size or 0)})...")
        try:
            local_path = download_file(url)
            actual_size = os.path.getsize(local_path)
            actual_name = os.path.basename(local_path)
            dl_elapsed = time.time() - dl_start
            log(f"  Downloaded: {fmt_size(actual_size)} in {dl_elapsed:.0f}s")
        except RuntimeError as e:
            msg = str(e)
            if is_quota(msg):
                log(f"\n  QUOTA EXCEEDED mid-download! Stopping.")
                log(f"  {processed} files done this run.")
                break
            log(f"  Download failed: {msg[:200]}")
            print(f"::error::download failed: {msg[:200]}")
            continue

        # If metadata was missing, use values from downloaded file
        if not metadata_ok:
            filename = actual_name
            file_size = actual_size
            if file_size > QUOTA_MAX:
                log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB")
                oversized.append({
                    "url": url, "filename": filename,
                    "size": file_size, "target_folder": active_folder
                })
                state["oversized"] = oversized
                save_completed(state)
                shutil.rmtree(TEMP_DIR, ignore_errors=True)
                continue
            if quota_used + file_size > QUOTA_MAX:
                log(f"  Quota full after download ({fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB)")
                log(f"  Processing this file anyway (already downloaded), then stopping.")

        quota_exhausted = (quota_used + file_size) >= QUOTA_MAX
        quota_used += file_size

        # Upload
        ul_start = time.time()
        log(f"  UPLOADING: \"{filename}\" ({fmt_size(file_size)}) to GDrive/{BASE_FOLDER}/{active_folder}/...")
        ensure_gdrive_folder(active_folder)
        try:
            uploaded_name = upload_file(local_path, active_folder)
            ul_elapsed = time.time() - ul_start
            log(f"  Uploaded: \"{uploaded_name}\" ({fmt_size(file_size)} in {ul_elapsed:.0f}s)")
        except RuntimeError as e:
            log(f"  Upload failed: {str(e)[:200]}")
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            continue

        # Verify (retry once on failure)
        log(f"  Verifying...")
        verified = verify_upload(uploaded_name, file_size, active_folder)
        if not verified:
            log(f"  Verification failed, retrying...")
            time.sleep(5)
            try:
                uploaded_name = upload_file(local_path, active_folder)
            except RuntimeError:
                pass
            verified = verify_upload(uploaded_name, file_size, active_folder)

        if verified:
            log(f"  VERIFIED: \"{uploaded_name}\" ({fmt_size(file_size)})")
        else:
            log(f"  Could not verify \"{uploaded_name}\" after retry")
            log(f"  Still marking complete (file is on GDrive, prevents duplicate)")

        # Always mark complete after upload succeeds (prevents re-upload duplicates)
        completed.append({
            "url": url,
            "filename": uploaded_name,
            "size": file_size,
            "target_folder": active_folder,
            "completed_at": timestamp()
        })
        folders[active_folder]["done"] += 1
        state["completed"] = completed
        state["folders"] = folders
        save_completed(state)
        log(f"  Artifact saved: {folders[active_folder]['done']}/{folders[active_folder]['total']} done")

        # Cleanup
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

        processed += 1
        log(f"  [{idx}/{total}] Complete | Quota: {fmt_size(quota_used)}/{fmt_size(QUOTA_MAX)}")
        log(f"  {'-' * 50}")

        if quota_exhausted:
            log(f"  Quota exhausted — remaining files will be processed next run.")
            break

    # Folder completion check
    fdata = folders[active_folder]
    if fdata["done"] >= fdata["total"]:
        fdata["status"] = "completed"
        log(f"\n  FOLDER COMPLETE: [{active_folder}] - {fdata['done']}/{fdata['total']} files")
        next_folder = None
        for name, fd in folders.items():
            if fd["status"] == "pending":
                fd["status"] = "active"
                next_folder = name
                break
        if next_folder:
            state["current_folder"] = next_folder
            fd_next = folders[next_folder]
            log(f"  Next folder: [{next_folder}] - {fd_next['done']}/{fd_next['total']}")
        else:
            state["current_folder"] = None
            log(f"  ALL FOLDERS COMPLETE! Sab kaam ho gaya!")
    else:
        log(f"\n  [{active_folder}] Progress: {fdata['done']}/{fdata['total']}")

    state["folders"] = folders
    state["completed"] = completed
    state["oversized"] = oversized
    save_completed(state)

    # Summary
    log(f"\n{'=' * 55}")
    log(f"  RUN SUMMARY")
    log(f"  {'-' * 55}")
    log(f"  Processed: {processed} files")
    log(f"  Quota used: {fmt_size(quota_used)} / {fmt_size(QUOTA_MAX)}")
    for name, fd in folders.items():
        icon = "DONE" if fd["status"] == "completed" else "ACTIVE" if fd["status"] == "active" else "WAIT"
        log(f"  [{icon}] {name}: {fd['done']}/{fd['total']}")
    if oversized:
        log(f"  OVERSIZED (>5GB): {len(oversized)} files - manual handling needed")
    log("=" * 55)

    remaining = sum(fd["total"] - fd["done"] for fd in folders.values() if fd["status"] != "completed")
    if remaining > 0:
        log(f"\n  {remaining} files remaining - next cycle will continue")
        # Signal to workflow that more runs needed
        print("::notice::More files pending - next cycle will continue")
    else:
        log(f"\n  SAB KAAM HO GAYA! :tada:")


if __name__ == "__main__":
    main()
