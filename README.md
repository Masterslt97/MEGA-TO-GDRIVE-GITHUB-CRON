# MEGA to Google Drive Transfer

Automatically transfer files from MEGA to Google Drive using GitHub Actions. Handles MEGA's bandwidth quota by auto-resuming after cooldown.

## How It Works

```
Script runs for 7 minutes → Quota hit? → EXIT
                                    ↓
                         Cron restarts after 1 min
                                    ↓
                         Script resumes from state file
                                    ↓
                         Repeat until all files done
```

- **Sequential transfer** — One file at a time: Download → Upload → Verify → Next
- **State tracking** — `mega_transfer_state.json` saves progress, never re-downloads
- **Auto-resume** — Quota hit = exit, next cron run picks up where it left off
- **Speed graph** — Live download speed (MB/s) with progress bar

## Features

| Feature | Description |
|---------|-------------|
| 🟢 Download + Upload | File transferred to GDrive successfully |
| 🟡 Skip | File already exists in GDrive |
| 🔴 Failed | Transfer failed after retries |
| ⚡ Speed bar | Live download speed (MB/s) |
| ⏱️ 7 min cycle | Runs 7 min, pauses 1 min, repeats |
| 🔒 Concurrency lock | Only one transfer runs at a time |

## Setup (5 minutes)

### Step 1: Create GitHub Repo

1. Go to [github.com/new](https://github.com/new)
2. Name: `mega-to-gdrive` (or anything)
3. Click **Create repository**

### Step 2: Upload Files

Upload these 2 files:
```
mega_to_gdrive.py              ← root
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
  ✅ [a8pA3SjC] '41105_720.mp4' (176.2 MB) — DONE
  🟢 Done: 1  🟡 Skip: 0  🔴 Fail: 0
  [████████████░░░░░░░░░░░░░░░░░░] ⚡ 27.3 MB/s  (1/208)
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
| `rclone copy failed` | Rclone remote name must be `gdrive` |
| Files not in Drive | Check if `RCLONE_CONF` has correct token |
| Quota error | Normal! Script exits, resumes in 1 min |
| `None` file names | Metadata fetch failed, still downloads correctly |

## Files

```
mega-to-gdrive/
├── .github/workflows/
│   └── mega_gdrive_transfer.yml    ← GitHub Actions workflow
├── mega_to_gdrive.py               ← Main transfer script
├── mega_transfer_state.json        ← Auto-generated progress tracker
└── README.md                       ← This file
```

## License

Free to use. Made by Shivam.
