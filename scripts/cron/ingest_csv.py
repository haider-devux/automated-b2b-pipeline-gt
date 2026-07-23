"""
bot-csv — the safe Fiverr / Upwork / LinkedIn lane (DEPLOYMENT_PLAN.md §4.5).

Scans <repo>/inbox/*.csv, imports each through the existing WF-1 CSV importer (wf1.py <csv>), then
moves the file to inbox/done/. This is how leads from platforms we must NOT scrape enter the pipeline:
a human/tool exports a CSV and drops it in inbox/. Zero flag risk — our server never touches them.

Run by systemd hourly (granjur-csv.timer). Safe to run when the folder is empty (no-op).
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # scripts/cron/ -> repo root
INBOX = ROOT / "inbox"
DONE = INBOX / "done"
FAILED = INBOX / "failed"


def main():
    INBOX.mkdir(exist_ok=True)
    DONE.mkdir(exist_ok=True)
    FAILED.mkdir(exist_ok=True)
    csvs = sorted(p for p in INBOX.glob("*.csv") if p.is_file())
    if not csvs:
        print("[csv] inbox empty — nothing to import.")
        return
    wf1_dir = ROOT / "wf1_python"
    for f in csvs:
        print(f"[csv] importing {f.name} ...")
        # sys.executable is the venv python systemd launched us with; run wf1.py from its own folder
        # so its `import db` resolves. wf1.py dedupes by domain, so re-imports are idempotent.
        r = subprocess.run([sys.executable, "wf1.py", str(f.resolve())], cwd=str(wf1_dir))
        dest = (DONE if r.returncode == 0 else FAILED) / f.name
        try:
            shutil.move(str(f), str(dest))
        except Exception as e:  # noqa: BLE001 — leave the file for a retry if the move fails
            print(f"[csv] could not archive {f.name}: {e}")
        status = "done -> inbox/done" if r.returncode == 0 else "FAILED -> inbox/failed"
        print(f"[csv] {f.name}: {status}")


if __name__ == "__main__":
    main()
