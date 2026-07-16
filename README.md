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
- [Upload Strategy](#upload-strategy)
- [Log Output Examples](#log-output-examples)
- [Troubleshooting](#troubleshooting)
- [Files](#files)

---

## How It Works

### The Problem

MEGA free accounts have a **~5GB daily download quota**. GitHub Actions runners are ephemeral — every run gets a fresh VM with a new IP, which means **fresh 5GB quota every run**. The system exploits this to transfer large amounts of data across multiple runs.

### The Solution

Instead of scanning GDrive every run (slow, 10-30 sec), we use **GitHub Artifacts** + **git** to maintain a persistent `completed_links.json` file that tracks:

- Which **files** are already uploaded (per URL)
- Which **folders** are active/completed/pending
- Which **folder** is currently being processed
- Which **oversized files** (>5GB) need manual handling

### Key Changes from v1

| Change | Old Way | New Way |
|--------|---------|---------|
| **Metadata fetch** | `megadl --info` (CLI flag unsupported in older versions) | `mega.py` Python library (`get_public_url_info()`) |
| **Download** | `megadl --progress` (CLI flag unsupported) | `mega.py` Python library (`download_url()`) |
| **Upload verify** | `rclone lsjson` check after upload (could timeout/crash) | No verify — upload directly marks complete. Upload always succeeds or raises error |
| **State save** | End of workflow via artifact + git | **Per-file git push** — har file ke baad immediate commit+push to repo |
| **Auto-trigger** | Always runs (even on cancellation) | Only on `success()` or `failure()`, not on cancellation |
| **Inline Python** | Inline `python -c "..."` in YAML (broke YAML parsing) | Standalone `auto_trigger.py` file |
| **Python compat** | — | `asyncio.coroutine` fallback for Python 3.12+ |
| **Schedule** | Cron every 5 minutes | No cron — only manual + auto-trigger when files remain |

---

## Complete Flow Diagram

### Main Workflow (Top-Level View)

```
    ┌──────────────────────────────────────────┐
    │    MANUAL TRIGGER or AUTO-TRIGGER        │
    │    (No cron - only on demand)            │
    └─────────────────┬────────────────────────┘
                      │ Trigger
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 1: SETUP                          │
    │  Install megatools + rclone + Python     │
    │  pip install mega.py                     │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │  PHASE 2: LOAD STATE                     │
    │  git pull → completed_links.json         │
    │  + Download artifact (backup)            │
    │  First run = {"folders":{}               │
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
    │  After each file: git push state         │
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
    │  3. Git commit + push (final backup)     │
    │  4. Auto-trigger? Only if not cancelled  │
    │     (if: success() || failure())         │
    │  5. Auto-stop if all folders done        │
    └─────────────────┬────────────────────────┘
                      │
                      ▼
    ┌──────────────────────────────────────────┐
    │            WORKFLOW END                  │
    └──────────────────────────────────────────┘
```

### Per-File Processing (Detailed)

When Phase 4 starts, each file goes through these steps:

```
    ┌─────────────────────────────────────────────────────┐
    │              START PROCESSING ONE FILE               │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP A: GET FILE INFO (via mega.py)                │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ Mega().get_public_url_info(url)               │  │
    │  │ Returns: (filename, size_bytes)               │  │
    │  │ No download — pure metadata (~1-2 sec)        │  │
    │  │ Fallback to megadl --info if mega.py fails    │  │
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
    │  STEP D: DOWNLOAD FROM MEGA (via mega.py)           │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ Mega().download_url(url, dest_path=TEMP_DIR)  │  │
    │  │ Fallback: megadl --path TEMP_DIR <url>        │  │
    │  │ File saved: TEMP_DIR/<filename>               │  │
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
    │  │    rclone copy <file> <gdrive:/>              │  │
    │  │ 3. Verify: no verify needed                   │  │
    │  │    (Upload always succeeds or raises error)   │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP F: SAVE STATE + GIT PUSH                      │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ 1. Append to completed_links.json:            │  │
    │  │    - url, filename, size                      │  │
    │  │    - target_folder, completed_at              │  │
    │  │ 2. git add + git commit + git push            │  │
    │  │    (Per-file push = CRASH-PROOF)              │  │
    │  │    Runner dies bhi → state already in repo!   │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  STEP G: CLEANUP & LOG                              │
    │  ┌───────────────────────────────────────────────┐  │
    │  │ 1. Delete: TEMP_DIR/*                         │  │
    │  │ 2. Print: "5/10 done | Quota: 3.4/5.0 GB"    │  │
    │  └───────────────────────────────────────────────┘  │
    └─────────────────────────┬───────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────┐
    │  Return to "More files?" check in Main Diagram      │
    └─────────────────────────────────────────────────────┘
```

### Multi-Run Progression Example

```
        RUN 1                        RUN 2                        RUN 3                        RUN 4
  ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
  │ State: empty     │      │ Git: 5 done      │      │ Git: 10 done    │      │ Git: 13 done    │
  │                  │      │ (per-file push)  │      │ (per-file push) │      │ (per-file push) │
  │ Bollywood: 10   │      │ Bollywood: 10   │      │ Bollywood:10 ✅ │      │ Hollywood:5 ✅  │
  │ Hollywood: 5    │      │ Hollywood: 5    │      │ Hollywood:5 ▶️ │      │                  │
  │                  │      │                  │      │                  │      │                  │
  │ Process: 1-5    │      │ Process: 6-10   │      │ Process: 1-3    │      │ Process: 4-5    │
  │ (quota: 4.8GB)  │      │ (quota: 4.2GB)  │      │ (quota: 3.1GB)  │      │ (quota: 2.1GB)  │
  │                  │      │                  │      │                  │      │                  │
  │ Bollywood:5/10  │      │ Bollywood:10/10 │      │ Hollywood:3/5   │      │ Hollywood:5/5   │
  │ Hollywood: wait │      │ Hollywood: wait │      │                  │      │                  │
  │                  │      │                  │      │  ⚡CRASH HERE?   │      │                  │
  │                  │      │                  │      │  No problem!    │      │                  │
  │                  │      │                  │      │  Git has 10/10  │      │                  │
  └──────────────────┘      └──────────────────┘      └──────────────────┘      └──────────────────┘
                                                                                                       │
                                                                                                       ▼
                                                                                              ┌──────────────────┐
                                                                                              │   ALL DONE !    │
                                                                                              │  30/30 files    │
                                                                                              └──────────────────┘
```
## Artifact System Explained

### What is an Artifact?

GitHub Actions **Artifacts** are files that persist **across workflow runs**. Unlike /tmp/ which is destroyed when a VM shuts down, artifacts stay on GitHub's servers for up to 90 days.

### How We Use Artifacts

```
Run 1:  [No artifact] → Create empty state → Process → Upload artifact
Run 2:  [Download artifact] → Read state → Process more → Upload (overwrite)
Run 3:  [Download artifact] → Read state → Process more → Upload (overwrite)
...
```

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
```

### Crash-Proof Design

Every successful file upload → **immediately saved to artifact + git push**:

```
Process File 1 → Save artifact ✅ + git push ✅ (state on GitHub)
Process File 2 → Save artifact ✅ + git push ✅ (state on GitHub)
Process File 3 → 💥 CRASH (VM dies, artifact upload never runs)
Next Run → git pull → File 1,2 already in state → Skip!
             → Start from File 3 (not from beginning!)

⚠️ Git push after EVERY file = TRUE crash-proof
   Artifact at end is backup — git is the real source of truth
```

---

## Features

| Feature | Description |
|---------|-------------|
| **Multi-folder** | JSON-based folder mapping. Each key = GDrive folder name. Auto-created via rclone mkdir. |
| **mega.py library** | Metadata fetch and download via Python library (more reliable than megadl CLI). |
| **asyncio.coroutine fallback** | Python 3.12+ compatibility fix for mega.py dependency. |
| **Per-file git push** | Har file upload ke baad turant `git commit + push`. Workflow crash ho tab bhi state safe. |
| **Smart quota** | Har file se pehle metadata fetch → size check. Agar quota exceed hone wala ho → skip gracefully. |
| **Oversized handling** | Files >5GB separated in oversized list. Manual handling ke liye alag category. |
| **Folder auto-advance** | Ek folder complete → next pending folder automatically active. |
| **Real-time logs** | Download/upload progress with MB, timing in GitHub Actions logs. |
| **Git backup** | Dual protection: Artifact + per-file git push. Har file ka record safe. |
| **Auto-trigger** | Files baki hain? → Next cycle automatically trigger via gh workflow run (skip on cancellation). |
| **Auto-stop** | Saare folders complete → no more cycles triggered. |
| **Multi-file merge** | Multiple `.txt` files ko merge karke ek single MEGA_LINKS secret banana (Python script included). |
| **Concurrency guard** | Only 1 run at a time — parallel runs prevented. |

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
```

### Step 2: Get Your rclone Config

If you don't have rclone configured for Google Drive:

`ash
# Install rclone (if not installed)
curl -s https://rclone.org/install.sh | sudo bash

# Configure Google Drive remote
rclone config
```

Follow the prompts:
```
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
```

After setup, view your config:
`ash
rclone config show gdrive
```

Copy the **entire output** — it looks like:
```
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
```

### Step 3: Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

#### Secret 1: MEGA_LINKS

Your MEGA file links in JSON format — one line (minified):

```
{"Bollywood Movies":["https://mega.nz/file/abc123#key1","https://mega.nz/file/def456#key2"],"Hollywood Movies":["https://mega.nz/file/ghi789#key3","https://mega.nz/file/jkl012#key4"]}
```

**Rules:**
- **Key** = GDrive folder name (automatically created)
- **Value** = Array of MEGA file links (not folder links)
- Multiple folders supported (separate with comma)
- Empty arrays allowed: `"FolderName": []`

**How to create MEGA_LINKS from a text file:**

Agar aapke paas ek `.txt` file hai jisme har line mein ek MEGA link hai:

```
https://mega.nz/file/abc#key1
https://mega.nz/file/def#key2
https://mega.nz/file/ghi#key3
```

Toh JSON convert karne ke liye ye Python command use karo:

```bash
python -c "import json; urls=[l.strip() for l in open('links.txt') if l.strip()]; print(json.dumps({'FolderName': urls}, separators=(',',':')))"
```

Output copy karo aur GitHub Secret mein paste karo. Example output:

```
{"FolderName":["https://mega.nz/file/abc#key1","https://mega.nz/file/def#key2","https://mega.nz/file/ghi#key3"]}
```

**Multi-Folder JSON from Multiple Text Files:**

Agar aapke paas **do alag folders ke liye do alag text files** hain, toh ek single merged JSON banana hoga:

```python
import json

# Har folder ke text file se URLs read karo
shorts_urls = [l.strip() for l in open('Shorts/MEGA_LINKS.txt') if l.strip()]
bg_urls = [l.strip() for l in open('.github/Beautiful Girls/MEGA_LINKS.txt') if l.strip()]

print(f'Shorts: {len(shorts_urls)} URLs')
print(f'Beautiful Girls: {len(bg_urls)} URLs')

# Merged JSON with two folders
merged = {
    "Shorts": shorts_urls,
    "Beautiful Girls": bg_urls
}

# Minified JSON output (GitHub Secret mein paste karna)
print(json.dumps(merged, separators=(',', ':')))
```

**Output — directly paste into GitHub Secret:**

```json
{"Shorts":["https://mega.nz/file/abc#key1","https://mega.nz/file/def#key2"],"Beautiful Girls":["https://mega.nz/file/xyz#key3","https://mega.nz/file/uvw#key4"]}
```

**Ek command mein (one-liner):**

```bash
python -c "import json; s=[l.strip() for l in open('Shorts/MEGA_LINKS.txt') if l.strip()]; b=[l.strip() for l in open('.github/Beautiful Girls/MEGA_LINKS.txt') if l.strip()]; print(json.dumps({'Shorts':s,'Beautiful Girls':b}, separators=(',',':')))"
```

#### Secret 2: RCLONE_CONF

Paste the **entire output** of 
clone config show gdrive

```
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
```

### Step 4: Run the Workflow

1. Go to your repo's **Actions** tab
2. Click **"MEGA to Google Drive Transfer"** in the left sidebar
3. Click the **"Run workflow"** button
4. Watch the logs in real-time

The workflow will NOT run automatically on a schedule. You must either:
- Click **"Run workflow"** manually from the Actions tab, or
- Let the **auto-trigger** start the next cycle when files remain after a run

**Auto-trigger behavior:**
- Runs only on `success()` or `failure()` — NOT on cancellation
- Automatically stops when all folders are complete (`auto_trigger.py`)
- Concurrency group ensures only 1 run at a time

---

## Secret Formats

### MEGA_LINKS (JSON Format)

**Correct format (one line, minified):**
`json
{"FolderA":["https://mega.nz/file/abc#key","https://mega.nz/file/def#key"],"FolderB":["https://mega.nz/file/ghi#key"]}
```

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
```

**❌ Wrong format (plain text — will cause JSON parse error):**
```
https://mega.nz/file/abc123#key1
https://mega.nz/file/def456#key2
```

---

## Resetting Completion List (State Reset)

### Kab reset karna chahiye?
- Pehli baar setup kar rahe hain
- GDrive se saari files delete karke fresh start karna chahte hain
- Koi corruption hui hai state file mein (e.g., merge conflict markers)

### Reset kaise karein?

**Method 1: GitHub API se (recommended)**
```
gh api -X PUT repos/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON/contents/completed_links.json \
  -f message="reset state [skip ci]" \
  -f content="eyJmb2xkZXJzIjoge319" \
  -f sha=COMMIT_SHA \
  -f branch=main
```
(COMMIT_SHA = current file SHA, `gh api repos/.../contents/completed_links.json --jq '.sha'` se milega)

**Method 2: Direct push (local repo)**
```bash
python -c "import json; json.dump({'folders':{}}, open('completed_links.json','w'), indent=2)"
git add completed_links.json
git commit -m "reset state [skip ci]"
git push
```

**Method 3: GitHub Web UI**
1. `completed_links.json` file open karo
2. ✏️ Edit button click karo
3. Content replace karo with: `{"folders": {}}`
4. "Commit changes" click karo (auto-commit to main)

### Reset ke baad kya hota hai?
- Saare folders wapas `pending` state mein aa jayenge
- `current_folder` null ho jayega
- Agli run first folder se start hogi, saari files from scratch process hogi

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

```
Run starts: quota_used = 0GB, quota_max = 5GB

File 1: size = 1.2GB → 0 + 1.2 = 1.2 ≤ 5 → ✅ Download + Upload
File 2: size = 2.3GB → 1.2 + 2.3 = 3.5 ≤ 5 → ✅ Download + Upload
File 3: size = 1.8GB → 3.5 + 1.8 = 5.3 > 5 → ⏭️ Skip (next run)
File 4: size = 800MB → (Not checked, loop already broke)
```

---

## Folder Auto-Advance

### How It Works

1. Script reads MEGA_LINKS JSON → discovers folders from `{"folders": {}}` state
2. Each folder gets state: `pending` → `active` → `completed`
3. First folder in JSON is auto-marked `active`
4. When active folder's `done >= total`:
   - Mark folder `completed`
   - Find next `pending` folder
   - Mark it `active`
   - Update `current_folder` in state
5. If no pending folders remain → **ALL DONE!** → auto-trigger stops

### State Propagation

```
MEGA_LINKS JSON (secret)             completed_links.json (state)
+-----------------------+             +----------------------------+
| {                     |     --->    | "folders": {              |
|   "Shorts": [45 URLs] |             |   "Shorts": {             |
| }                     |             |     "total": 45,          |
+-----------------------+             |     "done": 16,           |
                                      |     "status": "active"    |
                                      |   }                       |
                                      | }                          |
                                      | "completed": [...]        |
                                      | "current_folder": "Shorts"|
                                      | "oversized": [...]        |
                                      +----------------------------+
```

### Visualization

```
Initial:  Shorts [pending]
          | (auto-activate first)
Run 1-3:  Shorts [>> active] --- 16/45 done (quota hit)
          | (auto-trigger next run)
Run 4-5:  Shorts [>> active] --- 32/45 done
          |
Run 6:    Shorts [>> active] --- 45/45 done
          | (folder complete)
Final:    Shorts [OK done] --- ALL DONE!
```

---


## Upload Strategy

### No Verification Needed

Previous versions used `rclone lsjson` to verify each upload. This was **removed** because:

1. **Upload always succeeds or raises error** — rclone copy returns non-zero on failure
2. **Timeouts caused crashes** — `rclone lsjson` could timeout (30s) and crash the script mid-batch
3. **Per-file git push** provides the real crash-proofing — state saved to git before next file

### What happens after upload:

```
rclone copy <local_file> gdrive:MEGA_Transfer/<folder>/
if rclone returns 0 → upload succeeded → save state + git push
if rclone returns non-zero → RuntimeError → skip to next file (TEMP_DIR cleaned)
```

---

## Log Output Examples

### Normal Run (Mid-Progress)

```
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
```

### Quota Exhausted

```
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/ghi789...
  [Bollywood Movies] "Tenet.mp4" | Size: 2.0 GB
  Quota full: 3.4 GB + 2.0 GB = 5.4 GB > 5GB
  Skipping "Tenet.mp4" for this run
```

### Oversized File Detected

```
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/xyz789...
  [Bollywood Movies] "BigVideo_6GB.mp4" | Size: 6.0 GB
  OVERSIZED: BigVideo_6GB.mp4 (6.0 GB) > 5GB
```

### Folder Complete

```
  FOLDER COMPLETE: [Bollywood Movies] - 10/10 files
  Next folder: [Hollywood Movies] - 0/8
```

### All Done

```
  ALL FOLDERS COMPLETE! Sab files transfer ho gayi!
```

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| MEGA_LINKS is not valid JSON | Secret is plain text, not JSON | Convert links to {"Folder":["url1","url2"]} format minified |
| RCLONE_CONF secret is empty | Secret not set | Add RCLONE_CONF with output of `rclone config show gdrive` |
| Artifact download warning in first run | No artifact exists yet | Normal! continue-on-error: true handles it |
| Quota hit mid-download | MEGA bandwidth exhausted | Expected! Next run gets fresh quota |
| File stuck in "pending" but already in GDrive | State file corrupted/lost | Check completed_links.json in git — reset state if needed |
| Upload fails with 403 | Token expired | rclone auto-refreshes token |
| Folder not appearing in GDrive | Remote name wrong | Default remote is gdrive, must match rclone config |
| Workflow cancels but new one starts | Auto-trigger ran on cancellation | Fixed! Now uses `if: success() || failure()` |
| Files >5GB never get processed | MEGA quota limit per run | Download manually and upload via rclone |
| State file corrupted/merge conflict | Git pull --rebase conflict in completed_links.json | Reset state using methods in "Resetting Completion List" section |
| 422 error on workflow_dispatch | YAML parse error (inline Python broke YAML) | Fixed! Python code extracted to auto_trigger.py |
| mega.py ImportError / asyncio.coroutine error | Python 3.12+ removed coroutine() | Fixed! Script adds fallback: `asyncio.coroutine = lambda c: c` |
| Merged JSON mein links count mismatch | Multiple text files se merge karte waqt total galat ho raha | Ensure each text file ke URLs processed ho rahe hain — Python script se count check karo |

---

## Files

```
MEGA-TO-GDRIVE-GITHUB-CRON/
├── .github/
│   └── workflows/
│       └── mega_gdrive_transfer.yml       ← GitHub Actions workflow (manual + auto-trigger)
├── mega_to_gdrive.py                      ← Main transfer script (all logic)
├── auto_trigger.py                        ← Auto-trigger next cycle if files remain
├── completed_links.json                   ← State file (auto-generated, per-file git push)
├── MEGA_LINKS.json                        ← Shorts folder links (example)
├── MEGA_LINKS_Beautiful_Girls.json        ← Beautiful Girls folder links (example)
├── MEGA_LINKS_merged.json                 ← Merged JSON from multiple text files
├── .github/Shorts/MEGA_LINKS.txt          ← Source text file for Shorts
├── .github/Beautiful Girls/MEGA_LINKS.txt ← Source text file for Beautiful Girls
├── .gitignore                             ← Ignores TEMP_DIR (downloads)
└── README.md                              ← This file
```

### File Responsibilities

| File | What It Does |
|------|-------------|
| mega_to_gdrive.py | Reads secrets, manages state, downloads via mega.py, uploads to GDrive via rclone, per-file git push |
| auto_trigger.py | Checks completed_links.json for remaining files; triggers next gh workflow run if needed; auto-stops when all folders complete |
| mega_gdrive_transfer.yml | Defines GitHub Actions workflow: manual trigger, artifact steps, git backup, auto-trigger (skip on cancel) |
| completed_links.json | Persistent state: tracks folders, completed files, current folder, oversized files; updated per-file via git push |
| MEGA_LINKS.json | Shorts folder links (example source for MEGA_LINKS secret) |
| MEGA_LINKS_Beautiful_Girls.json | Beautiful Girls folder links (example source) |
| MEGA_LINKS_merged.json | Merged JSON from multiple text files — ready to paste into GitHub Secret |
| .github/Shorts/MEGA_LINKS.txt | Raw text file for Shorts (one URL per line) |
| .github/Beautiful Girls/MEGA_LINKS.txt | Raw text file for Beautiful Girls (one URL per line) |
| .gitignore | Prevents TEMP_DIR/ download directory from being committed to git |

---

## Architecture Summary

```
   ┌─────────────────────────────────────────────────────────────┐
   │                    GITHUB ACTIONS RUNNER                    │
   │                    (Ephemeral Linux VM)                     │
   │                                                             │
   │   ┌─────────────────────────────────────────────────────┐   │
   │   │  WORKFLOW (mega_gdrive_transfer.yml)                 │   │
   │   │                                                     │   │
   │   │  1. Checkout repo                                   │   │
   │   │  2. Install megatools + mega.py + rclone            │   │
   │   │  3. git pull + download artifact (state)            │   │
   │   │  4. Run mega_to_gdrive.py  <- main logic            │   │
   │   │     (per-file git push inside script!)              │   │
   │   │  5. Upload artifact (overwrite - backup)            │   │
   │   │  6. Git commit + push (final - may be no-op)       │   │
   │   │  7. Auto-trigger (skip if cancelled)               │   │
   │   │  8. Auto-stop if all folders done                  │   │
   │   └─────────────────────────────────────────────────────┘   │
   │                             │                                │
   │                             v                                │
   │   ┌─────────────────────────────────────────────────────┐   │
   │   │  PYTHON SCRIPT (mega_to_gdrive.py)                   │   │
   │   │                                                     │   │
   │   │  Load state -> Find active folder                   │   │
   │   │         │                                            │   │
   │   │         v                                            │   │
   │   │  For each pending file:                             │   │
   │   │    +-- Get metadata (mega.py get_public_url_info)    │   │
   │   │    +-- Check oversized (>5GB?) -> skip if yes        │   │
   │   │    +-- Check quota (<=5GB?) -> skip if no            │   │
   │   │    +-- Download (mega.py download_url)               │   │
   │   │    +-- Upload (rclone copy)                          │   │
   │   │    +-- Save + git push (per-file = crash-proof!)    │   │
   │   │    +-- Cleanup temp files                            │   │
   │   │         │                                            │   │
   │   │         v                                            │   │
   │   │  Folder done? -> Auto-advance to next                │   │
   │   └─────────────────────────────────────────────────────┘   │
   └───────────────────────────┬─────────────────────────────────┘
                               │
           ┌───────────────────┴───────────────────┐
           │                   │                   │
           v                   v                   v
   +---------------+   +---------------+   +---------------+
   |  MEGA CLOUD   |   | GDRIVE CLOUD  |   |GITHUB REPO   |
   |               |   |               |   |  + ARTIFACT  |
   |  Source via   |   |  Destination  |   |               |
   |  mega.py API  |   |  MEGA_Transfer|   |  State file   |
   |  ~5GB quota   |   |  /{Folder}/   |   |  per-file     |
   |  per IP/day   |   |               |   |  git push     |
    +---------------+   +---------------+   +---------------+
```
---

## License

Free to use. Made by Shivam.




