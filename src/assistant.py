"""AI negotiation coach — answers a patient's free-text question grounded in THEIR
analyzed bill, using Claude Fable 5 (Anthropic's most capable model) with an Opus 4.8
refusal-fallback. Gated on ANTHROPIC_API_KEY; returns "" when the key is absent so the
endpoint degrades gracefully. anthropic is imported lazily.
"""
from __future__ import annotations

import os

MODEL = "claude-fable-5"
SYSTEM = (
    "You are a warm, practical medical-bill negotiation coach for US hospital bills; the "
    "patient's state is given in the analysis. "
    "Use ONLY the bill analysis provided in the user's message. Every figure is an ESTIMATE "
    "from public Medicare rates and hospital-published prices — not a guarantee, and not "
    "legal, medical, or financial advice. Answer the patient's question using the specific "
    "numbers from their bill; when it helps, give exact sentences they can say or write to "
    "the billing office. Be concrete and encouraging. If the analysis doesn't contain what's "
    "needed, say so and suggest what to ask the hospital for. Keep answers under ~200 words "
    "and end with a one-line reminder that these are estimates, not a guarantee."
)


def build_context(rows, issues, state):
    """Compact plain-text summary of the analyzed bill to ground the model."""
    out = [f"Bill analysis ({state}; estimates from public Medicare + hospital price data):"]
    for r in rows:
        if not r.get("resolved"):
            continue
        d = r["result"]
        seg = [f"- {r['code']} {d.get('description') or d.get('code_type') or ''}:".rstrip()]
        if d.get("your_price") is not None:
            seg.append(f"charged ${d['your_price']:,.0f};")
        if not d.get("medicare_missing"):
            seg.append(f"Medicare ${d['medicare_rate']:,.0f};")
        if d.get("neg_median") is not None:
            seg.append(f"local fair price ${d['neg_median']:,.0f};")
        if d.get("target_ask") is not None:
            seg.append(f"suggested target ${d['target_ask']:,.0f};")
        if d.get("allowed") is not None:
            seg.append(f"plan-allowed ${d['allowed']:,.0f};")
        out.append(" ".join(seg))
    if issues:
        out.append("Flags: " + "; ".join(i["title"] for i in issues) + ".")
    return "\n".join(out)


def _text(resp):
    if getattr(resp, "stop_reason", None) == "refusal":
        return ("I can't help with that specific question, but I'm happy to help you "
                "understand or negotiate any of the charges on your bill.")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def ask(question, context):
    """Return the coach's answer, or "" if the feature isn't configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    import anthropic

    client = anthropic.Anthropic()
    msgs = [{"role": "user", "content": f"{context}\n\nPatient's question: {question}"}]
    try:
        return _text(client.beta.messages.create(
            model=MODEL, max_tokens=1200,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": "claude-opus-4-8"}],   # transparent rescue if Fable declines
            output_config={"effort": "medium"},
            system=SYSTEM, messages=msgs))
    except Exception:
        # ponytail: SDK/param drift on a public endpoint shouldn't 500 — retry minimal, then bail.
        try:
            return _text(client.messages.create(
                model=MODEL, max_tokens=1200, system=SYSTEM, messages=msgs))
        except Exception:
            return ("The AI coach is temporarily unavailable — the talking points and "
                    "negotiation letter above still have everything you need to start.")
