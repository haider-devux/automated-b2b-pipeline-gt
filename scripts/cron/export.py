"""
bot-export — nightly Excel snapshot + central-database update (granjur-export.timer).

Thin wrapper around scripts/export_leads_csv.py:export_xlsx(conn) so it can run as its own cron unit,
independent of the big run_pipeline.py orchestrator.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import governor  # noqa: E402


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    exporter = _load("scripts/export_leads_csv.py", "export_leads_csv_cron")
    conn = governor.connect()
    try:
        path, n = exporter.export_xlsx(conn=conn)
        print(f"[export] wrote snapshot: {n} compan(ies) -> {path}")
        print(f"[export] updated central database -> {ROOT / 'exports' / 'granjur_central.xlsx'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
