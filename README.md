# MEGA to Google Drive Transfer

Multi-folder file transfer system from MEGA to Google Drive using GitHub Actions. Uses **artifact-based state tracking** to survive crashes and **smart quota management** to handle MEGA's 5GB/day bandwidth limit.

---

## 📋 Table of Contents

- [How It Works](#how-it-works)
- [Complete Flow Diagram](#complete-flow-diagram)
- [Artifact System Explained](#artifact-system-explained)
- [Features](#features)
- [Setup Guide](#setup-guide)
- [Secret Formats](#secret-formats)
- [How Quota Is Managed](#how-quota-is-managed)
- [Folder Auto-Advance](#folder-auto-advance)
- [Verification Logic](#verification-logic)
- [Log Output Examples](#log-output-examples)
- [Troubleshooting](#troubleshooting)
- [Files](#files)

---

## How It Works

### The Problem

MEGA free accounts have a **~5GB daily download quota**. GitHub Actions runners are ephemeral — every run gets a fresh VM with a new IP, which means **fresh 5GB quota every run**. The system exploits this to transfer large amounts of data across multiple runs.

### The Solution

Instead of scanning GDrive every run (slow, 10-30 sec), we use **GitHub Artifacts** to maintain a persistent completed_links.json file that tracks:

- Which **files** are already uploaded (per URL)
- Which **folders** are active/completed/pending
- Which **folder** is currently being processed
- Which **oversized files** (>5GB) need manual handling

---

## Complete Flow Diagram

### Main Workflow (Top-Level View)

`
    ┌──────────────────────────────────────────┐
    │          GITHUB ACTIONS CRON              │
    │          Runs every 5 minutes             │
    └─────────────────┬────────────────────────┘
                      │ Trigger
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 1: SETUP                          │
    │  Install megatools + rclone + Python     │
    │  Write rclone.conf from secret           │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 2: LOAD STATE                     │
    │  Download artifact (completed_links.json)│
    │  First run = empty = no error            │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 3: PREPARE                        │
    │  Parse MEGA_LINKS secret (JSON format)   │
    │  Identify active folder                  │
    │  Filter already-completed URLs           │
    │  Separate oversized files (>5GB)         │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 4: PROCESS ONE FILE               │
    │  (See "Per-File Processing" below)       │
    └─────────────────┬────────────────────────┘
                      │
         ┌────────────┴────────────┐
         ▼                         ▼
    ┌──────────┐            ┌──────────────┐
    │ More     │            │ No more      │
    │ files?   │──YES──────▶│ files /      │
    │          │            │ quota full?  │
    └──────────┘            └──────┬───────┘
         NO                        │ YES
         │                         │
         ▼                         ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 5: COMPLETE RUN                   │
    │  1. Check folder completion              │
    │     If done: mark complete               │
    │     Activate next folder                 │
    │  2. Upload artifact (overwrite)          │
    │  3. Git commit + push (backup)           │
    │  4. Trigger next cycle if pending        │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │            WORKFLOW END                  │
    └──────────────────────────────────────────┘
`

### Per-File Processing (Detailed)

When Phase 4 starts, each file goes through these steps:

`
    ┌─────────────────────────────────────────────────────┐
    │              START PROCESSING ONE FILE               │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP A: GET FILE INFO                              │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ Run: megadl --info <url>                      │  │
    │  │ Output: filename + file_size (bytes)          │  │
    │  │ No download yet - just metadata (~1-2 sec)    │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP B: OVERSIZED CHECK                            │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ Is file_size > 5GB?                           │  │
    │  └──────────┬────────────────────┬───────────────┘  │
    │             │ YES                │ NO               │
    │             ▼                    │                  │
    │  ┌──────────────────┐            │                  │
    │  │ Add to OVERSIZED │            │                  │
    │  │ list in artifact │            │                  │
    │  │ Skip forever     │            │                  │
    │  └──────────────────┘            │                  │
    └──────────────────────────────────┼──────────────────┘
                                       │ (only if NOT oversized)
                                       ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP C: QUOTA CHECK                                │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ quota_used + file_size > 5GB?                 │  │
    │  └──────────┬────────────────────┬───────────────┘  │
    │             │ YES                │ NO               │
    │             ▼                    ▼                  │
    │  ┌──────────────────┐    ┌──────────────────────┐   │
    │  │ Skip this file   │    │ quota_used += size   │   │
    │  │ Next run retries │    │ Start download →     │   │
    │  └──────────────────┘    └──────────────────────┘   │
    └─────────────────────────────────────────────────────┘
                              │ (only if quota OK)
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP D: DOWNLOAD FROM MEGA                         │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ megadl --progress --path /tmp/mega_temp       │  │
    │  │ Shows: % complete, speed (MB/s), ETA          │  │
    │  │ File saved: /tmp/mega_temp/<filename>         │  │
    │  │ If quota exceeded mid-download → graceful exit│  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP E: UPLOAD TO GOOGLE DRIVE                     │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ 1. Create folder if not exists:               │  │
    │  │    rclone mkdir gdrive:MEGA_Transfer/Folder   │  │
    │  │ 2. Upload file:                               │  │
    │  │    rclone copy --progress <file> <gdrive:/>   │  │
    │  │ Shows: % complete, speed (MB/s), ETA          │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP F: VERIFY UPLOAD                              │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ rclone lsjson gdrive:.../<filename>           │  │
    │  │                                                │  │
    │  │ Check: filename EXACTLY match?                 │  │
    │  │        file size EXACTLY match?                │  │
    │  │ (Not a full GDrive scan - just 1 file check)   │  │
    │  └──────────┬────────────────────┬───────────────┘  │
    │             │ YES                │ NO               │
    │             ▼                    ▼                  │
    │  ┌──────────────────┐    ┌──────────────────────┐   │
    │  │ Upload VERIFIED  │    │ Retry upload 1 time  │   │
    │  │ ✅ Proceed       │    │ Still fail? → skip   │   │
    │  └──────────────────┘    │ and continue         │   │
    │                          └──────────────────────┘   │
    └─────────────────────────────────────────────────────┘
                              │ (only if verified OK)
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP G: SAVE TO ARTIFACT                           │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ Append to completed_links.json:               │  │
    │  │ - url, filename, size                         │  │
    │  │ - target_folder, completed_at                 │  │
    │  │ Save to disk immediately!!!                    │  │
    │  │ (Per-file save = crash-proof design)          │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP H: CLEANUP & LOG                              │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ 1. Delete: /tmp/mega_temp/<file>              │  │
    │  │ 2. Print: "5/10 done | Quota: 3.4/5.0 GB"    │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  Return to "More files?" check in Main Diagram      │
    └─────────────────────────────────────────────────────┘
`

### Multi-Run Progression Example

`
        RUN 1                        RUN 2                        RUN 3                        RUN 4
  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
  │ Artifact: empty  │      │ Artifact: 5 done │      │ Artifact:10 done │      │ Artifact:13 done │
  │                  │      │                  │      │                  │      │                  │
  │ Bollywood: 10   │      │ Bollywood: 10   │      │ Bollywood:10 ✅ │      │ Hollywood:5 ✅  │
  │ Hollywood: 5    │      │ Hollywood: 5    │      │ Hollywood:5 ▶️ │      │                  │
  │                  │      │                  │      │                  │      │                  │
  │ Process: 1-5    │      │ Process: 6-10   │      │ Process: 1-3    │      │ Process: 4-5    │
  │ (quota: 4.8GB)  │      │ (quota: 4.2GB)  │      │ (quota: 3.1GB)  │      │ (quota: 2.1GB)  │
  │                  │      │                  │      │                  │      │                  │
  │ Bollywood:5/10  │      │ Bollywood:10/10 │      │ Hollywood:3/5   │      │ Hollywood:5/5   │
  │ Hollywood: wait │      │ Hollywood: wait │      │                  │      │                  │
  └──────────────────┘      └──────────────────┘      └──────────────────┘      └──────────────────┘
                                                                                                      │
                                                                                                      ▼
                                                                                             ┌──────────────────┐
                                                                                             │   ALL DONE !    │
                                                                                             │  30/30 files    │
                                                                                             └──────────────────┘
`
## Artifact System Explained

### What is an Artifact?

GitHub Actions **Artifacts** are files that persist **across workflow runs**. Unlike /tmp/ which is destroyed when a VM shuts down, artifacts stay on GitHub's servers for up to 90 days.

### How We Use Artifacts

`
Run 1:  [No artifact] → Create empty state → Process → Upload artifact
Run 2:  [Download artifact] → Read state → Process more → Upload (overwrite)
Run 3:  [Download artifact] → Read state → Process more → Upload (overwrite)
...
`

### Artifact File Structure (completed_links.json)

`json
{
  "folders": {
    "Bollywood": {
      "total": 10,
      "done": 5,
      "status": "active"
    },
    "Hollywood": {
      "total": 8,
      "done": 0,
      "status": "pending"
    }
  },
  "completed": [
    {
      "url": "https://mega.nz/file/abc#key123",
      "filename": "Interstellar.mp4",
      "size": 2454900000,
      "target_folder": "Bollywood",
      "completed_at": "2025-01-15T10:30:00Z"
    }
  ],
  "current_folder": "Bollywood",
  "oversized": [
    {
      "url": "https://mega.nz/file/xyz#key456",
      "filename": "BigFile_6GB.mp4",
      "size": 6442450944,
      "target_folder": "Bollywood"
    }
  ]
}
`

### Crash-Proof Design

Every successful file upload → **immediately saved to artifact** on disk:

`
Process File 1 → Save artifact ✅
Process File 2 → Save artifact ✅
Process File 3 → 💥 CRASH (VM dies)
Next Run → Download artifact → Files 1,2 are already completed → Skip!
             → Start from File 3 (not from beginning!)
`

---

## Features

| Feature | Description |
|---------|-------------|
| **Multi-folder** | JSON-based folder mapping. Each key = GDrive folder name. Auto-created via rclone mkdir. |
| **Artifact state** | Per-file artifact save. Crash-proof — agli run wahi se resume karega. |
| **No full GDrive scan** | Single-file clone lsjson verification (1-2 sec per file vs 10-30 sec for full scan). |
| **Smart quota** | Har file se pehle metadata fetch → size check. Agar quota exceed hone wala ho → skip gracefully. |
| **Oversized handling** | Files >5GB separated in oversized list. Manual handling ke liye alag category. |
| **Folder auto-advance** | Ek folder complete → next pending folder automatically active. |
| **Real-time logs** | Download/upload progress with %, speed, ETA in GitHub Actions logs. |
| **Git backup** | Dual protection: Artifact + git commit. Agar artifact lost ho, git se restore. |
| **Auto-trigger** | Files baki hain? → Next cycle automatically trigger via gh workflow run. |

---

## Setup Guide

### Prerequisites

- A **GitHub account**
- A **MEGA account** with files to transfer
- A **Google Drive** account (any, even free 15GB)
- **rclone configured** with Google Drive (one-time setup)

---

### Step 1: Fork / Clone This Repository

`ash
git clone https://github.com/your-username/MEGA-TO-GDRIVE-GITHUB-CRON.git
cd MEGA-TO-GDRIVE-GITHUB-CRON
`

### Step 2: Get Your rclone Config

If you don't have rclone configured for Google Drive:

`ash
# Install rclone (if not installed)
curl -s https://rclone.org/install.sh | sudo bash

# Configure Google Drive remote
rclone config
`

Follow the prompts:
`
n) New remote
name> gdrive
Storage> drive
client_id> (press Enter for default)
client_secret> (press Enter for default)
scope> 1 (Full access)
root_folder_id> (press Enter)
service_account_file> (press Enter)
Edit advanced config? n
Use auto config? y
`

After setup, view your config:
`ash
rclone config show gdrive
`

Copy the **entire output** — it looks like:
`
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
`

### Step 3: Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

#### Secret 1: MEGA_LINKS

Your MEGA file links in JSON format — one line (minified):

`
{"Bollywood Movies":["https://mega.nz/file/abc123#key1","https://mega.nz/file/def456#key2"],"Hollywood Movies":["https://mega.nz/file/ghi789#key3","https://mega.nz/file/jkl012#key4"]}
`

**Rules:**
- **Key** = GDrive folder name (automatically created)
- **Value** = Array of MEGA file links (not folder links)
- Multiple folders supported
- Empty arrays allowed: "FolderName": []

#### Secret 2: RCLONE_CONF

Paste the **entire output** of clone config show gdrive

`
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
`

### Step 4: Run the Workflow

1. Go to your repo's **Actions** tab
2. Click **"MEGA to Google Drive Transfer"** in the left sidebar
3. Click the **"Run workflow"** button
4. Watch the logs in real-time

The workflow will also run automatically via cron every 5 minutes.

---

## Secret Formats

### MEGA_LINKS (JSON Format)

**Correct format (one line, minified):**
`json
{"FolderA":["https://mega.nz/file/abc#key","https://mega.nz/file/def#key"],"FolderB":["https://mega.nz/file/ghi#key"]}
`

**Readable version (for understanding, do NOT use in secret):**
`json
{
  "Bollywood Movies": [
    "https://mega.nz/file/abc123#key1",
    "https://mega.nz/file/def456#key2"
  ],
  "Hollywood Movies": [
    "https://mega.nz/file/ghi789#key3"
  ]
}
`

**❌ Wrong format (plain text — will cause JSON parse error):**
`
https://mega.nz/file/abc123#key1
https://mega.nz/file/def456#key2
`

---

## How Quota Is Managed

### The Problem

MEGA free accounts limit download bandwidth to approximately **5GB per day** per IP. When exceeded, downloads fail with "quota exceeded" errors.

### How This System Solves It

| Mechanism | Description |
|-----------|-------------|
| **Fresh VM = Fresh Quota** | Every GitHub Actions run gets a new VM with a new IP — MEGA sees it as a new user with full quota |
| **Pre-check before download** | megadl --info fetches file size without downloading. If current run's remaining quota < file size → skip gracefully |
| **Graceful exit** | When quota nears exhaustion, script exits cleanly. Artifact is already saved → next run resumes |
| **Per-run limit** | Script tracks quota_used in memory. Once it exceeds 5GB, stops processing more files |

### Example Quota Scenario

`
Run starts: quota_used = 0GB, quota_max = 5GB

File 1: size = 1.2GB → 0 + 1.2 = 1.2 ≤ 5 → ✅ Download + Upload
File 2: size = 2.3GB → 1.2 + 2.3 = 3.5 ≤ 5 → ✅ Download + Upload
File 3: size = 1.8GB → 3.5 + 1.8 = 5.3 > 5 → ⏭️ Skip (next run)
File 4: size = 800MB → (Not checked, loop already broke)
`

---

## Folder Auto-Advance

### How It Works

1. Script reads MEGA_LINKS JSON → discovers folders
2. Each folder gets state: pending → ctive → completed
3. First folder in JSON is auto-marked ctive
4. When active folder's done == total:
   - Mark folder completed
   - Find next pending folder
   - Mark it ctive
   - Update current_folder in artifact
5. If no pending folders remain → **ALL DONE!**

### Visualization

`
Initial:  Bollywood [pending]  Hollywood [pending]  WebSeries [pending]
          ↓ (auto-activate first)
Run 1-3:  Bollywood [▶️ active]   Hollywood [pending]   WebSeries [pending]
          ↓ (Bollywood done)
Run 4:    Bollywood [✅ done]     Hollywood [▶️ active]  WebSeries [pending]
          ↓ (Hollywood done)
Run 5:    Bollywood [✅ done]     Hollywood [✅ done]    WebSeries [▶️ active]
          ↓ (WebSeries done)
Final:    ALL DONE! 🎉
`

---

## Verification Logic

### How We Verify Uploads

Instead of scanning the entire GDrive folder (slow), we verify **one file at a time**:

`python
rclone lsjson gdrive:MEGA_Transfer/Bollywood/Interstellar.mp4
`

Returns:
`json
[{"Name": "Interstellar.mp4", "Size": 2454900000, ...}]
`

**We check two things:**
1. **Filename** = Exact match
2. **Size** = Exact match (bytes)

**Both must match** → upload verified ✅

### Why This Is Reliable

- Two different files **cannot** have the same name + same size in the same folder
- MEGA links provide unique filenames per link
- No full GDrive scan needed (saves 10-30 sec per run)

---

## Log Output Examples

### Normal Run (Mid-Progress)

`
=======================================================
  MEGA -> GDrive Transfer | 2025-01-15 10:30:00
=======================================================
  Artifact loaded: 5 completed files, 0 oversized
  Total pending: 15
-------------------------------------------------------
  [ACTIVE] Bollywood Movies: 5/10
  [WAIT] Hollywood Movies: 0/8
-------------------------------------------------------

  Active: [Bollywood Movies] -> 5 files pending
=======================================================

  --- [1/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/abc123...
  [Bollywood Movies] "Interstellar.mp4" | Size: 2.3 GB
  DOWNLOADING: "Interstellar.mp4"...
  Downloaded: 2.3 GB
  UPLOADING to GDrive/MEGA_Transfer/Bollywood Movies/...
  Uploaded: "Interstellar.mp4"
  Verifying...
  VERIFIED: "Interstellar.mp4" (2.3 GB)
  Artifact saved: 6/10 done
  [1/5] Complete | Quota: 2.3/5.0 GB
  --------------------------------------------------

  --- [2/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/def456...
  [Bollywood Movies] "Inception.mp4" | Size: 1.1 GB
  DOWNLOADING: "Inception.mp4"...
  Downloaded: 1.1 GB
  UPLOADING...
  Uploaded: "Inception.mp4"
  VERIFIED: "Inception.mp4" (1.1 GB)
  Artifact saved: 7/10 done
  [2/5] Complete | Quota: 3.4/5.0 GB
  --------------------------------------------------

  ... (more files) ...

  [Bollywood Movies] Progress: 7/10

=======================================================
  RUN SUMMARY
  --------------------------------------------------
  Processed: 2 files
  Quota used: 3.4 GB / 5.0 GB
  [ACTIVE] Bollywood Movies: 7/10
  [WAIT] Hollywood Movies: 0/8
=======================================================

  8 files remaining - next cycle will continue
`

### Quota Exhausted

`
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/ghi789...
  [Bollywood Movies] "Tenet.mp4" | Size: 2.0 GB
  Quota full: 3.4 GB + 2.0 GB = 5.4 GB > 5GB
  Skipping "Tenet.mp4" for this run
`

### Oversized File Detected

`
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/xyz789...
  [Bollywood Movies] "BigVideo_6GB.mp4" | Size: 6.0 GB
  OVERSIZED: BigVideo_6GB.mp4 (6.0 GB) > 5GB
`

### Folder Complete

`
  FOLDER COMPLETE: [Bollywood Movies] - 10/10 files
  Next folder: [Hollywood Movies] - 0/8
`

### All Done

`
  ALL FOLDERS COMPLETE! Sab files transfer ho gayi!
`

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| MEGA_LINKS is not valid JSON | Secret is plain text, not JSON | Convert links to {"Folder":["url1","url2"]} format minified |
| RCLONE_CONF secret is empty | Secret not set | Add RCLONE_CONF with output of clone config show gdrive |
| Artifact download warning in first run | No artifact exists yet | Normal! continue-on-error: true handles it |
| Quota hit mid-download | MEGA bandwidth exhausted | Expected! Next run gets fresh quota |
| File stuck in "pending" but already in GDrive | Artifact lost, URL not marked done | Check completed_links.json in git backup |
| Upload fails with 403 | Token expired | rclone auto-refreshes token |
| Folder not appearing in GDrive | Remote name wrong | Default remote is gdrive, must match rclone config |
| Workflow keeps running after all done | No "all done" detection | Script outputs ::notice:: — check logs for "ALL DONE" |
| Files >5GB never get processed | MEGA quota limit per run | Download manually and upload via rclone |

---

## Files

`
MEGA-TO-GDRIVE-GITHUB-CRON/
├── .github/
│   └── workflows/
│       └── mega_gdrive_transfer.yml    ← GitHub Actions workflow (cron schedule, artifact steps)
├── mega_to_gdrive.py                   ← Main transfer script (all logic)
├── completed_links.json                ← Artifact state file (auto-generated, git-tracked)
├── .gitignore                          ← Ignores mega_temp/ (downloads)
└── README.md                           ← This file
`

### File Responsibilities

| File | What It Does |
|------|-------------|
| mega_to_gdrive.py | Reads secrets, manages state, downloads from MEGA, uploads to GDrive via rclone, verifies, saves artifact |
| mega_gdrive_transfer.yml | Defines GitHub Actions workflow: triggers via cron, handles artifacts, git backup, auto-trigger next cycle |
| completed_links.json | Persistent state: tracks folders, completed files, current folder, oversized files |
| .gitignore | Prevents mega_temp/ download directory from being committed to git |

---

## Architecture Summary

`
┌─────────────────────────────────────────────────────────────────┐
│                    GITHUB ACTIONS RUNNER                        │
│                    (Ephemeral Linux VM)                         │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  GITHUB WORKFLOW                                         │   │
│  │  ├── Checkout repo                                       │   │
│  │  ├── Install megatools, rclone, Python                   │   │
│  │  ├── Download artifact (completed_links.json)            │   │
│  │  ├── Run mega_to_gdrive.py                               │   │
│  │  ├── Upload artifact (overwrite completed_links.json)    │   │
│  │  ├── Git commit + push (backup)                          │   │
│  │  └── gh workflow run (trigger next cycle)                │   │
│  └─────────────────────────────────────────────────────────┘   │
│                               │                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  mega_to_gdrive.py (Python)                              │   │
│  │                                                          │   │
│  │  1. Load completed_links.json (artifact)                 │   │
│  │  2. Parse MEGA_LINKS secret (JSON)                       │   │
│  │  3. Find active folder → filter pending URLs             │   │
│  │  4. FOR EACH pending URL:                                │   │
│  │     ├── megadl --info → filename + size                  │   │
│  │     ├── size > 5GB? → Mark oversized → skip forever      │   │
│  │     ├── quota_used + size > 5GB? → Break (next run)      │   │
│  │     ├── megadl --progress → download to /tmp/mega_temp   │   │
│  │     ├── rclone mkdir → ensure GDrive folder exists       │   │
│  │     ├── rclone copy --progress → upload to GDrive        │   │
│  │     ├── rclone lsjson → verify filename + size match     │   │
│  │     ├── Append to completed_links.json (per-file save!)  │   │
│  │     ├── Delete temp file                                 │   │
│  │     └── Log progress                                     │   │
│  │  5. Check folder completion → auto-advance if needed     │   │
│  │  6. Save final state                                     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                               │                                  │
└───────────────────────────────┼─────────────────────────────────┘
                                │
           ┌────────────────────┼────────────────────┐
           ▼                    ▼                    ▼
    ┌──────────┐         ┌──────────┐         ┌──────────┐
    │  MEGA    │         │  GDRIVE  │         │ GITHUB   │
    │  Cloud   │         │  Cloud   │         │ ARTIFACT │
    │  (source)│         │  (dest)  │         │ (state)  │
    └──────────┘         └──────────┘         └──────────┘
`

---

## License

Free to use. Made by Shivam.

