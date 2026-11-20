"""Shared sampling constants for local llama.cpp LLM calls.

Qwen non-thinking preset: temperature 0.7, top-p 0.8, top-k 20 — per the
Qwen3 technical report's non-thinking sampling guidance (arXiv 2505.09388,
p.13) and Unsloth's Qwen3 serving documentation.

Penalty parameters are deliberately absent (plan D3): the llama-server
process owns ``--presence-penalty 1.5``; sending penalties client-side would
double-apply.
"""

QWEN_SAMPLING: dict[str, float | int] = {"temperature": 0.7, "top_p": 0.8, "top_k": 20}
