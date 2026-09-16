"""OCR a medical bill / EOB image or PDF into the lines our bill parser expects,
using Claude vision (Haiku 4.5 — cheap, fast, good enough for extraction).

Gated on ANTHROPIC_API_KEY: if it's not set, extract_eob returns "" and the app
shows a "not enabled" message instead of erroring. anthropic is imported lazily so
the app runs (and tests pass) without the package when the feature is off.
"""
from __future__ import annotations

import base64
import os

MODEL = "claude-haiku-4-5"
PROMPT = (
    "You are extracting billing lines from a medical bill or insurance EOB. "
    "Output ONE line per service, format:  CODE  description  $charge  $allowed\n"
    "- CODE is the CPT/HCPCS/DRG code (append a modifier as CODE-NN if shown).\n"
    "- $charge is the amount billed; $allowed is the plan-allowed amount from an EOB "
    "if present (omit it if not). Format amounts with a $ and commas.\n"
    "Example:  73721 MRI lower extremity $2,400 $900\n"
    "Output ONLY the lines — no header, no commentary. If you find no billing codes, "
    "output nothing."
)


def extract_eob(data: bytes, content_type: str) -> str:
    """Return extracted bill lines, or "" if the feature isn't configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    import anthropic

    b64 = base64.standard_b64encode(data).decode()   # no newlines
    if "pdf" in (content_type or "").lower():
        block = {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        media = content_type if (content_type or "").startswith("image/") else "image/jpeg"
        block = {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}

    msg = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()
