# MEGA to Google Drive Transfer

Automatically transfer files from MEGA to Google Drive using GitHub Actions. Handles MEGA's bandwidth quota by auto-resuming after cooldown.

## How It Works

```
Script runs for 7 minutes → Download → Upload to GDrive → Next file
                                         ↓
                              Quota hit? → EXIT
                                         ↓
                              Cron restarts after 1 min
                                         ↓
                              Script resumes (skips done files)
                                         ↓
                              Repeat until all files done
```

- **Sequential transfer** — One file at a time: Download → Upload → Next
- **Skip check** — Already uploaded files are skipped (saves MEGA bandwidth)
- **State tracking** — `mega_transfer_state.json` saved to repo, never re-downloads
- **Auto-resume** — Quota hit = exit, next cron run picks up where it left off
- **Speed bar** — Live download speed (MB/s) with visual progress
- **Google Drive API** — Direct upload via API (no rclone copy issues)

## Features

| Feature | Description |
|---------|-------------|
| 🟢 Download + Upload | File transferred to GDrive successfully |
| 🟡 Skip | File already exists in GDrive (bandwidth saved!) |
| 🔴 Failed | Transfer failed after retries |
| ⚡ Speed bar | Live download speed (MB/s) |
| ⏱️ 7 min cycle | Runs 7 min, pauses 1 min, repeats |
| 🔒 Concurrency lock | Only one transfer runs at a time |
| 📁 State persistence | Progress saved to repo, survives restarts |
| 🔄 Token auto-refresh | Google Drive token refreshes automatically |

## Output Example

```
═══════════════════════════════════════════════════
  🟢 MEGA -> Google Drive Transfer
  Total: 208 | Pending: 180
  ⏱️  Run for 7 min, then pause 1 min
  🏁 Stop at: 12:09:30
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

## Setup (5 minutes)

### Step 1: Create GitHub Repo

1. Go to [github.com/new](https://github.com/new)
2. Name: `mega-to-gdrive` (or anything)
3. Click **Create repository**

### Step 2: Upload Files

Upload these files:
```
mega_to_gdrive.py              ← root
mega_transfer_state.json       ← root (empty: {})
.github/workflows/
  mega_gdrive_transfer.yml     ← inside .github/workflows/
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

## How Skip Check Works

1. **State file** — Checks `mega_transfer_state.json` for completed transfers
2. **GDrive scan** — Lists files on GDrive via API, compares name + size
3. **Skip** — If file exists on GDrive, marks as 🟡 skip (no download needed)

This saves MEGA bandwidth by not re-downloading already transferred files.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `RCLONE_CONF empty` | Secret name must be exactly `RCLONE_CONF` |
| `No valid MEGA links` | Links must be one per line in `MEGA_LINKS` secret |
| `401 Unauthorized` | Token expired — auto-refreshes now |
| `403 Forbidden` | Rate limit — script retries with backoff |
| `VERIFY FAILED` | File not on remote after upload — check permissions |
| Quota error | Normal! Script exits, resumes in 1 min |
| `None` file names | Metadata fetch failed, still downloads correctly |
| Files not in GDrive | Check `RCLONE_CONF` has valid refresh_token |

## Files

```
mega-to-gdrive/
├── .github/workflows/
│   └── mega_gdrive_transfer.yml    ← GitHub Actions workflow
├── mega_to_gdrive.py               ← Main transfer script
├── mega_transfer_state.json        ← Progress tracker (auto-updated)
├── .gitignore                      ← Ignores mega_temp/
└── README.md                       ← This file
```

## How It Uploads

Uses **Google Drive API** directly (not rclone copy):
1. Gets fresh access token from rclone config (auto-refresh)
2. Creates/finds `MEGA_Transfer` folder on GDrive
3. Resumable upload via API
4. Verifies file ID returned

## License

Free to use. Made by Shivam.
