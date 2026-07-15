#!/usr/bin/env python3
"""Remove duplicate files from GDrive MEGA_Transfer folder, keep only 1 copy."""
import subprocess
import sys

GDRIVE_REMOTE = "gdrive"
GDRIVE_FOLDER = "MEGA_Transfer"

def main():
    # List all files
    r = subprocess.run(
        ["rclone", "lsf", f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}", "--max-depth", "1"],
        capture_output=True, text=True, timeout=120
    )
    files = [line.strip().rstrip("/") for line in r.stdout.strip().splitlines()
             if line.strip() and not line.strip().endswith("/")]

    print(f"Total files: {len(files)}")

    # Count duplicates
    from collections import Counter
    counts = Counter(files)
    dupes = {k: v for k, v in counts.items() if v > 1}

    if not dupes:
        print("No duplicates found!")
        return

    print(f"\n{len(dupes)} files have duplicates:")
    removed = 0
    for name, count in sorted(dupes.items(), key=lambda x: -x[1]):
        # Keep first copy, remove rest
        to_remove = count - 1
        print(f"  {name}: {count} copies -> keeping 1, removing {to_remove}")
        # Remove duplicates using rclone deletefile
        for i in range(to_remove):
            cmd = ["rclone", "deletefile", f"{GDRIVE_REMOTE}:{GDRIVE_FOLDER}/{name}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                removed += 1
            else:
                print(f"    Failed to remove: {result.stderr[:100]}")

    print(f"\nRemoved {removed} duplicate files")

if __name__ == "__main__":
    main()
