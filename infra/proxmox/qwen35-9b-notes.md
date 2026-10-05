# Qwen3.5-9B Configuration (hybrid GatedDeltaNet + gated attention)

Operational notes for the llama.cpp server on LXC 112 (`quanfolio-llm`), serving
Quantfolio's local LLM. Cutover from Qwen3-8B dense completed 2026-08-25
(see `docs/archive/audits/2026-08-qwen35-postmortem.md`; prior-era notes live in git
history under the old filename `dense-8b-notes.md`).

## Hardware profile

- **GPU**: RTX 3070 8 GB (full offload, no CPU expert fallback)
- **System RAM (LXC 112)**: 16 GB
- **Measured VRAM @ 32K context**: 6903 MiB total used (weights ~5.6 GB +
  KV cache @32K ≈ 0.9 GB thanks to DeltaNet's near-flat KV growth + CUDA
  overhead) — gate is ≤ 7600 MiB
- **Target throughput**: ~55–60 tok/s generation, ~2700 tok/s prefill (measured)

## Model

**Qwen3.5-9B-UD-Q4_K_XL** (Unsloth Dynamic 2.0)

- Architecture: hybrid — Gated DeltaNet linear attention (~3:1) + gated full
  attention; native context **262,144** tokens, running at 32,768
- File: `/var/lib/quantfolio/models/Qwen3.5-9B-UD-Q4_K_XL.gguf` (5.96 GB)
- sha256: `6f5d30666c2d8ae16a306e616d95341dcf3cc46810df84d7e6f5a7d1e4c1b293`
- License: Apache 2.0 · thinking mode disabled server-side AND per-request

## Serving flags (must match `infra/systemd/quantfolio-llamacpp.service`)

```bash
/opt/llama.cpp/llama-server \
  --model /var/lib/quantfolio/models/Qwen3.5-9B-UD-Q4_K_XL.gguf \
  --host 0.0.0.0 --port 8080 \
  --ctx-size 32768 \
  --n-gpu-layers 99 \
  --flash-attn on \
  --threads 8 \
  --parallel 1 \
  --cont-batching --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.00 \
  --presence-penalty 1.5 \
  --no-context-shift \
  --reasoning-budget 0 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --metrics
```

Sampling preset mirrors `backend/app/services/llm/sampling.py`
(Qwen non-thinking guidance, arXiv 2505.09388 p.13). Thinking-off is enforced
belt-and-braces: server flag + request-level `chat_template_kwargs` sent by
the app + `--reasoning-budget 0`.

## History

| Era | Model | Notes |
|---|---|---|
| ≤ 2026-06 | Qwen3.6-35B-A3B (MoE) | `--cpu-moe`, needed 16 GB RAM, capped ctx 8K |
| 2026-06 → 2026-08-25 | Qwen3-8B dense (UD-Q4_K_XL) | full GPU, 16K ctx |
| 2026-08-25 → | **Qwen3.5-9B** (this doc) | full GPU, 32K ctx, schema-constrained output via app |

## Rollback assets (on LXC 112)

- `/etc/systemd/system/quantfolio-llamacpp.service.bak-qwen3-8b` — pre-cutover unit (sha256 `7f9986b6…`)
- `/etc/systemd/system/quantfolio-llamacpp.service.bak-qwen35-ctx16k` — post-cutover unit @16K ctx
- `/var/lib/quantfolio/models/Qwen3-8B-UD-Q4_K_XL.gguf` (4.8 GB) — old weights

## Links

- Unsloth Qwen3.5 GGUF: https://huggingface.co/unsloth/Qwen3.5-9B-GGUF
- Qwen3.5-9B model card: https://huggingface.co/Qwen/Qwen3.5-9B
- Baseline / postmortem audits: `docs/archive/audits/2026-08-qwen35-*.md`
