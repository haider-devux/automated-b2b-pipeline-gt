"""WF-4 config. Reuses WF-3's DB password (one place). Outreach knobs live in outreach.py so the
dashboard can import the payload builder without pulling in DB settings."""
import importlib.util
import pathlib

_wf3_config = pathlib.Path(__file__).resolve().parent.parent / "wf3_python" / "config.py"
_spec = importlib.util.spec_from_file_location("wf3_config", _wf3_config)
_wf3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wf3)
DB = _wf3.DB
