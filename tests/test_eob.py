"""Test the EOB OCR gating (no API key -> no call). `python tests/test_eob.py`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from eob import extract_eob  # noqa: E402


def test_no_key_returns_empty_without_calling_api():
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert extract_eob(b"any bytes", "image/png") == ""   # gated off, no network call
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
