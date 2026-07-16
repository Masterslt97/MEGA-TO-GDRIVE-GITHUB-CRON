# MEGA to Google Drive Transfer

Multi-folder file transfer from MEGA to Google Drive using GitHub Actions with artifact-based state tracking. Handles MEGA's 5GB bandwidth quota by auto-resuming across runs.

## How It Works

`
GitHub Actions triggers every 5 min
        ↓
Download artifact (completed_links.json)
        ↓
Read state → Find active folder → Filter pending links
        ↓
For each file: metadata → quota check → download → upload → verify → save artifact
        ↓
Folder complete? → Auto-advance to next folder → Upload artifact
        ↓
Git backup commit + trigger next cycle
`

## Features

| Feature | Description |
|---------|-------------|
| Multi-folder | JSON-based folder mapping, auto-advance on completion |
| Artifact state | Per-file artifact save (crash-proof, survives restarts) |
| No GDrive scan | Single-file rclone lsjson verification (1-2 sec per file) |
| Quota aware | Per-file size check before download, skip if >5GB remaining |
| Oversized handling | Files >5GB separated for manual handling |
| Real-time progress | Download/upload percentage, speed, ETA in logs |
| Git backup | Dual protection: artifact + git commit |

## Setup

### Step 1: Fork/Clone this repo

### Step 2: Add Secrets

**Settings → Secrets and variables → Actions → New repository secret**

#### Secret 1: MEGA_LINKS (JSON format)

Single-line minified JSON:
`json
{"FolderName1":["https://mega.nz/file/abc#key","https://mega.nz/file/def#key"],"FolderName2":["https://mega.nz/file/ghi#key"]}
`

Each key = GDrive folder name (auto-created), value = array of MEGA file links.

#### Secret 2: RCLONE_CONF

`ash
rclone config show gdrive
`

Copy entire output and paste as secret value.

### Step 3: Run

1. Go to **Actions** tab
2. Click **"MEGA to Google Drive Transfer"**
3. Click **"Run workflow"**
4. Check real-time logs

## State File (Artifact)

`
completed_links.json (auto-managed)
  ├── folders: {name, total, done, status}
  ├── completed: [{url, filename, size, target_folder, completed_at}]
  ├── current_folder: active folder name
  └── oversized: [{url, filename, size, target_folder}]
`

Har file ke baad artifact update hota hai — agar run beech mein crash ho, agli run wahi se resume karega.

## Example Log Output

`
  MEGA -> GDrive Transfer | 2025-01-15 10:30:00
  Artifact loaded: 5 completed files
  Total pending: 25
  [ACTIVE] Bollywood: 2/10
  [WAIT] Hollywood: 0/8
  [WAIT] Web Series: 0/7
  --- [1/10] Bollywood ---
  Fetching: https://mega.nz/file/abc...
  [Bollywood] "Interstellar.mp4" | Size: 2.3 GB
  DOWNLOADING: "Interstellar.mp4"...
  Downloaded: 2.3 GB
  UPLOADING to GDrive/MEGA_Transfer/Bollywood/...
  Uploaded: "Interstellar.mp4"
  VERIFIED: "Interstellar.mp4" (2.3 GB)
  Artifact saved: 3/10 done
  [1/10] Complete | Quota: 2.3/5.0 GB
`

## Quota & Cycle

| Aspect | Behavior |
|--------|----------|
| Quota per run | ~5 GB (new VM = new IP = fresh quota) |
| Cron schedule | */5 * * * * (every 5 minutes) |
| File skip logic | Before download: size check. If quota full → skip for this run |
| Oversized | Files >5GB marked in artifact, skipped automatically |
| Folder advance | One folder done → next pending folder auto-activates |
| Completion | All folders done → workflow stops triggering |

## Files

`
MEGA-TO-GDRIVE-GITHUB-CRON/
├── .github/workflows/
│   └── mega_gdrive_transfer.yml    ← GitHub Actions workflow
├── mega_to_gdrive.py               ← Main transfer script
├── completed_links.json            ← Artifact state file (auto-managed)
├── .gitignore                      ← Ignores mega_temp/
└── README.md                       ← This file
`

## Troubleshooting

| Problem | Solution |
|---------|----------|
| JSON parse error | MEGA_LINKS must be valid JSON, not plain text |
| Artifact not found | First run? Normal. continue-on-error handles it |
| Quota exceeded | Expected! Next run gets fresh 5GB quota |
| Upload failed | Check RCLONE_CONF is valid |
| Folder not created | rclone mkdir auto-creates — check remote name is "gdrive" |
| Duplicate uploads | Artifact tracks completed URLs — check completed_links.json |

## License

Free to use. Made by Shivam.
