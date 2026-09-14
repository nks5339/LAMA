# Local-model output quality — measured, before and after

Answers the question "if someone is using local models from Ollama, is the
generated code trustworthy?" with numbers from the real fabric rather than an
opinion.

Everything below was produced by calling a local model through
`fabric_chat` / `fabric_call` — the same path the product uses — not by a
mock.

| | |
|---|---|
| Model | `qwen2.5-coder:7b` (local Ollama, `http://localhost:11434/v1`) |
| Agent | `codegen.verifier`, the agent that gates whether a generated file is accepted |
| Prompt | A real Spring `@RestController` plus the verifier's own scoring instructions |
| Temperature | 0.1 |
| Runs | 6 per configuration |

---

## Result

The verifier's job is to return `{"verdict": ..., "confidence": ..., "issues": [...]}`.
The pipeline parses that with `_extract_json_object`. If parsing fails the task
is marked `VERIFY_FAILED` at confidence 0.0 and a good file is reported as bad.

| Configuration | Reply is valid JSON | Parsed by the OLD extractor | Parsed by the FIXED extractor |
|---|---|---|---|
| **A** — before any of this work | 0 / 6 | **0 / 6** | 6 / 6 |
| **B** — with `response_format` reaching the provider | **6 / 6** | 6 / 6 | 6 / 6 |

**Before this work the verifier lost every single verdict.** Not some. All six.

---

## Why it was 0 / 6

Two independent defects stacked, and each alone was enough to lose the verdict.

**The structured-output request never reached the provider.** Fifteen agent
call sites passed `response_format={"type":"json_object"}` into `fabric_call`.
`llm.py` forwarded only `max_tokens` and `temperature` to `fabric_chat`, so the
kwarg was dropped before any payload was built. Every structural agent asked for
strict JSON and silently got prose.

**So the model fenced its reply, and the extractor could not read a fence.**
Asked without JSON mode, `qwen2.5-coder:7b` replied identically all six times:

```
```json
{
  "verdict": "ACCEPT",
  "confidence": 100,
  "issues": []
}
```
```

`routes/codegen.py::_extract_json_object` stripped fences with
`s.split("```", 2)[-1]`, which on a correctly fenced block returns the empty
string that follows the **closing** fence, not the content between the fences:

```python
'```json\n{...}\n```'.split('```', 2)
# -> ['', 'json\n{...}\n', '']      and [-1] is ''
```

The brace scan that followed then had nothing to scan, so the function returned
`None` for the single most common shape an LLM emits.

`routes/tools.py` has its own copy of that helper which handles fences
correctly, so the Transformer pipeline was never affected — only multi-agent
CodeGen. The two remain separate per the ground rules and are catalogued as
duplicates in [RECON.md](../RECON.md); both are now pinned by
`backend/tests/test_extract_json_object.py` so they cannot diverge on this
behaviour again.

---

## What now protects local-model output

Four layers, in the order a reply passes through them.

**1. JSON mode reaches the provider, in that provider's own wire format.**
`apply_json_mode()` is the single translation point. Ollama's `/v1` surface
takes OpenAI's `response_format` field; Anthropic and unrecognised providers
get a system instruction instead, so a newly added provider degrades to a
prompt rather than 400-ing every structural call.

**2. The word "json" is guaranteed present when JSON mode is on.** OpenAI and
Azure reject `json_object` mode unless the messages contain the literal word.
Without this guard, enabling JSON mode would have converted working calls into
hard 400s for prompts that did not happen to say it.

**3. One bounded repair re-ask.** When a `response_format` call still comes
back unparseable, `fabric_call` re-asks exactly once, showing the model its own
output and the parser error. Capped at one, so a model that cannot produce JSON
costs one wasted round-trip rather than a loop. A failed repair returns the
original result, so it can only add a chance of success.

**4. The extractor handles fenced, bare and prose-wrapped replies.** The fast
path parses the whole reply; the fallback scans the outermost braces, which
covers fences, preambles, trailing chatter and a reply truncated mid-fence at
`max_tokens`.

Underneath those, the guards that already existed and were kept: the `num_ctx`
estimator, the context-window auto-upgrade, `_looks_like_placeholder` rejecting
empty-shell files, and `_sanitize_llm_file` stripping fence residue out of
generated source.

---

## Reproducing

```bash
ollama serve                       # must be reachable on :11434
ollama pull qwen2.5-coder:7b
BENCH_N=6 .venv/bin/python <bench script>
```

The benchmark script is not committed — it monkeypatches Mongo collections and
is a measurement tool, not a test. The behaviours it measures are pinned by
committed tests instead:

```bash
.venv/bin/python -m pytest \
  backend/tests/test_extract_json_object.py \
  backend/tests/test_json_mode_routing.py \
  backend/tests/test_json_repair.py -q
```

---

## Honest limits

- Six runs on one model with one prompt. Enough to establish that the old path
  failed **every** time and the new one succeeds, not enough to quote a
  percentage for other models.
- `qwen2.5-coder:7b` honours Ollama's `response_format` reliably. Smaller
  models may not, which is exactly the case the repair re-ask exists for; that
  path is unit-tested but is not exercised by these numbers because this model
  never needed it.
- This measures whether the verifier's **verdict survives transport**. It does
  not measure whether the verdict is correct — that is the accuracy of the model
  itself and is a separate question.
