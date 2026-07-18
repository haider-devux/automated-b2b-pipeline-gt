"""
WF-1 configuration. Reuses the DB password you already set in wf3_python/config.py
(one place for the credential; a shared package comes in the week-2 refactor).
"""
import importlib.util
import pathlib

_wf3_config = pathlib.Path(__file__).resolve().parent.parent / "wf3_python" / "config.py"
_spec = importlib.util.spec_from_file_location("wf3_config", _wf3_config)
_wf3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wf3)
DB = _wf3.DB

# Valid region codes (the region_code enum in the DB). Anything else -> OTHER.
VALID_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU", "OTHER"}

# A city x niche "cell" is considered depleted once new/seen drops below this (blueprint 1.4).
DEPLETION_THRESHOLD = 0.05
