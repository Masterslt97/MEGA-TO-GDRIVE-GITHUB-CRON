# MEGA to Google Drive Transfer

Automatically transfer files from MEGA to Google Drive using GitHub Actions. Handles MEGA's bandwidth quota by auto-resuming after cooldown.

## How It Works

```
Cron triggers every 1 minute
        ↓
Script loads state file → checks GDrive folder
        ↓
Skip already done files → Download pending from MEGA
        ↓
Upload to GDrive via API → Save state → Git push
        ↓
7 min window → EXIT → Cron triggers again
        ↓
Repeat until ALL files transferred
```

## Features

| Feature | Description |
|---------|-------------|
| 🟢 Download + Upload | File transferred to GDrive successfully |
| 🟡 Skip | File already exists in GDrive (bandwidth saved!) |
| 🔴 Failed | Transfer failed after retries |
| ⚡ Speed bar | Live download speed (MB/s) with progress bar |
| ⏱️ 7 min cycle | Runs 7 min, exits, cron restarts after 1 min |
| 📁 GDrive scan | Checks GDrive folder via rclone lsf before download |
| 📁 State persistence | Progress saved to repo via git, survives restarts |
| 🔄 Token auto-refresh | Google Drive token refreshes automatically |
| 🔁 403 retry | Auto-retries on rate limit with token refresh |
| 📊 Live counters | Running total of downloaded/skipped/failed files |

## Output Example

```
═══════════════════════════════════════════════════
  🟢 MEGA -> Google Drive Transfer
  Total: 208 | Pending: 180
  ⏱️  Run for 7 min, then pause 1 min
  🏁 Stop at: 15:01:59
═══════════════════════════════════════════════════

⬇️  [a8pA3SjC] downloading '41105_720.mp4' (176.2 MB)... [1/180]
  📥 Downloading...
  📤 Uploading to GDrive...
  🔑 Refreshing token with client_id: 202264815644.apps...
  ✅ Token refreshed successfully
  ✅ Uploaded to GDrive: 41105_720.mp4 (ID: 1bNnmEb4TVO...)
  ✅ [a8pA3SjC] '41105_720.mp4' (176.2 MB) — DONE
  🟢 Done: 1  🟡 Skip: 0  🔴 Fail: 0
  [████████████░░░░░░░░░░░░░░░░░░] ⚡ 27.3 MB/s  (1/208)
```

Skip (file already on GDrive):
```
⬇️  [utQT1DSJ] downloading '44573_720.mp4' (305.4 MB)... [2/180]
🟡 [utQT1DSJ] '44573_720.mp4' — skip (already in Drive)
  🟢 Done: 1  🟡 Skip: 1  🔴 Fail: 0
```

Quota hit:
```
🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴
🔴                                      🔴
🔴   QUOTA OVER 🚫 BANDWIDTH LIMIT      🔴
🔴   Wait 1 min — Cron auto-resumes!    🔴
🔴                                      🔴
🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴
```

All done:
```
🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢
🟢   HURRY 🎉 208 FILES TRANSFERRED     🟢
🟢   TO GDRIVE! WAh 🎉                  🟢
🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢
```

## Features

| Feature | Description |
|---------|-------------|
| 🟢 Download + Upload | File transferred to GDrive successfully |
| 🟡 Skip | File already exists in GDrive (bandwidth saved!) |
| 🔴 Failed | Transfer failed after retries |
| ⚡ Speed bar | Live download speed (MB/s) with progress |
| ⏱️ 7 min cycle | Runs 7 min, exits, cron restarts after 1 min |
| 📁 State persistence | Progress saved to repo via git, survives restarts |
| 🔄 Token auto-refresh | Google Drive token refreshes via refresh_token |
| 🔍 GDrive scan | Checks GDrive folder before downloading |
| 🔁 403 retry | Auto-retries on rate limit with token refresh |

## Setup (5 minutes)

### Step 1: Create GitHub Repo

1. Go to [github.com/new](https://github.com/new)
2. Name: `mega-to-gdrive` (or anything)
3. Click **Create repository**

### Step 2: Upload Files

Upload these files:
```
mega_to_gdrive.py              ← root
mega_transfer_state.json       ← root (contents: {})
.github/workflows/
  mega_gdrive_transfer.yml     ← inside .github/workflows/
.gitignore                     ← root (contents: mega_temp/)
```

### Step 3: Add Secrets

Go to: **Settings → Secrets and variables → Actions → New repository secret**

#### Secret 1: `MEGA_LINKS`
```
https://mega.nz/file/abc123#xyz
https://mega.nz/file/def456#uvw
https://mega.nz/file/ghi789#rst
```
(One link per line)

#### Secret 2: `RCLONE_CONF`

Get your rclone config:
```bash
rclone config show gdrive
```

Output looks like:
```
[gdrive]
type = drive
client_id = xxxxxx
client_secret = xxxxxx
scope = drive
token = {"access_token":"xxxxx","refresh_token":"xxxxx"}
```

Copy the **entire output** and paste as `RCLONE_CONF` secret.

### Step 4: Run

1. Go to **Actions** tab
2. Click **"MEGA to Google Drive Transfer"**
3. Click **"Run workflow"**
4. Done!

## How Skip Check Works (3 Levels)

1. **State file** — If `mega_transfer_state.json` has `status: completed` → skip
2. **GDrive scan** — `rclone lsf` lists GDrive folder, checks filename → skip
3. **MEGA metadata** — If filename from MEGA matches GDrive → skip + save state

This ensures no file is downloaded twice, even if state file was lost.

## How Upload Works

Uses **Google Drive API** directly (not rclone copy):
1. Gets fresh access token via `refresh_token` from rclone config
2. Creates/finds `MEGA_Transfer` folder on GDrive
3. Resumable upload via API
4. Returns file ID on success
5. Auto-retries on 403 with token refresh

## How Cycle Works

```
Cron triggers every 1 minute
        ↓
Script loads state + scans GDrive
        ↓
Skip done files → Download pending → Upload → Save state
        ↓
7 min timer starts
        ↓
Time up? → EXIT
        ↓
Cron triggers again → Resume from state
        ↓
Repeat until ALL DONE
```

## Rclone Config Setup (First Time)

If you don't have rclone configured yet:

```bash
# Install rclone
curl -s https://rclone.org/install.sh | sudo bash

# Setup
rclone config
```

Follow the prompts:
```
n) New remote
name> gdrive
Storage> drive
client_id> (press Enter)
client_secret> (press Enter)
scope> 1
root_folder_id> (press Enter)
service_account_file> (press Enter)
Edit advanced config? n
Use auto config? y
Configure as team drive? n
Yes this is OK
q) Quit
```

Then get config:
```bash
rclone config show gdrive
```

Copy output → paste as `RCLONE_CONF` secret.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `RCLONE_CONF empty` | Secret name must be exactly `RCLONE_CONF` |
| `No valid MEGA links` | Links must be one per line in `MEGA_LINKS` secret |
| `401 Unauthorized` | Token expired — auto-refreshes now |
| `403 Forbidden` | Rate limit — script retries with backoff (10s/20s) |
| `VERIFY FAILED` | File not on remote after upload |
| Quota error | Normal! Script exits, resumes in 1 min |
| `None` file names | Metadata fetch failed, still downloads correctly |
| Files not in GDrive | Check `RCLONE_CONF` has valid refresh_token |
| Same file uploaded twice | Check scan_drive returns files, duplicates cleaned |
| Runs cancelled | Remove concurrency lock from workflow |

## Files

```
mega-to-gdrive/
├── .github/workflows/
│   └── mega_gdrive_transfer.yml    ← GitHub Actions workflow
├── mega_to_gdrive.py               ← Main transfer script
├── mega_transfer_state.json        ← Progress tracker (auto-updated)
├── cleanup_duplicates.py           ← One-time cleanup script
├── check_duplicates.py             ← Check for duplicate files
├── .gitignore                      ← Ignores mega_temp/
└── README.md                       ← This file
```

## How Token Refresh Works

```
rclone config → has refresh_token
        ↓
Script calls googleapis.com/token
        ↓
Gets new access_token (expires in 1 hour)
        ↓
Uses for API calls
        ↓
On 401/403 → refresh again → retry
```

## License

Free to use. Made by Shivam.
