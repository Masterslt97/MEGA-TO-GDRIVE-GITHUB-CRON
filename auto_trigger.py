import json, subprocess, sys

try:
    d = json.load(open('completed_links.json'))
except Exception:
    d = {'folders': {}}

rem = sum(
    f['total'] - f['done']
    for f in d.get('folders', {}).values()
    if f.get('status') != 'completed'
)

if rem > 0:
    r = subprocess.run(
        ['gh', 'workflow', 'run', 'MEGA to Google Drive Transfer', '--ref', 'main'],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f'Triggered next cycle ({rem} files remaining)')
    else:
        print(f'Trigger failed: {r.stderr.strip()}')
        sys.exit(1)
else:
    print('All done, no more cycles')
