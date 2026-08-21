import importlib.util
from pathlib import Path


def _load():
    candidates = [
        Path(__file__).resolve().parent.parent / "nws_common.py",
        Path(__file__).resolve().parent / "nws_common_embedded.py",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("nws_common_payload_shared", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError("No bundled NWS common module was found")


_mod = _load()

for _name in dir(_mod):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_mod, _name)
