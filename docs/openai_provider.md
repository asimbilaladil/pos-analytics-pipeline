# OpenAI alongside Claude in Laynes Intelligence

Users can pick an OpenAI GPT model as well as Claude in the chat model picker.
Claude remains the default. OpenAI is **off** until it is explicitly configured.

## What changes and what does not

`chat_providers.py` is only the transport to the model. These all stay in
`chat_sql.answer_question` and run the same way for both providers:

- the `check_data` and `run_sql` tools;
- the pglast validator, the relation allowlist and the `laynes_ro` role;
- `enforce_scope` and the domain trust gates (Revel / HME / labor / identity / R365 / R365 financial);
- the R365 posting notes (no posting is not $0);
- FINANCIAL_WASTE_REVIEW's 3-query budget, together with its SQL-computed targets and hypothesis sentence;
- the blocked-path verdict;
- forced synthesis and `EMPTY_ANSWER_FALLBACK`.

OpenAI never gets a database tool or connection of its own, and no hosted tools
(web search, code interpreter, file search).

| | Claude | OpenAI |
|---|---|---|
| API | Messages | Responses (`openai==3.35.1`), `store=false`, encrypted reasoning passed back each turn |
| Tool loop call | unchanged: `max_tokens` 4096, model-default thinking | `reasoning.effort` low (or `OPENAI_REASONING_EFFORT` if the model supports it), `max_output_tokens` 12000 |
| Final synthesis | no tools, thinking disabled, 4096 | `tool_choice: "none"`, the lowest effort the model accepts (`none` or `low`), 4096 |
| Timeout / retries | 90 s / 1 | 90 s / 1 (`CHAT_OPENAI_TIMEOUT_SECONDS`, `CHAT_OPENAI_MAX_RETRIES`) |

The OpenAI loop budget is set per provider and does not change the Claude budget.
On reasoning models, `max_output_tokens` also covers the hidden reasoning. A turn
that hits the budget is still caught (`status: incomplete`, reason
`max_output_tokens`) and goes to forced synthesis, the same as a Claude
`max_tokens` turn. Reasoning-only output is never treated as an answer.

## Supported models

A model is offered only when all three of these hold:

1. It is listed in `OPENAI_ALLOWED_MODELS`.
2. It has a parameter profile in `chat_providers.OPENAI_MODEL_PROFILES`, because reasoning-effort support differs by model and a wrong value is an HTTP 400.
3. `models.retrieve` confirms the configured key can use it. This call returns metadata only, is not billed, and is cached for 10 minutes.

Profiles, verified 2026-10-19 against developers.openai.com and openai-python 3.35.1:

| id | label | efforts | synthesis effort |
|---|---|---|---|
| gpt-6-astra | GPT-6 Astra | low..max | low |
| gpt-6.1-sol | GPT-6.1 Sol | low..max | low |
| gpt-6-sol | GPT-6 Sol | none, low..max | none |
| gpt-6-luna | GPT-6 Luna | none, low..max | none |

Any other id in the allowlist is ignored and logged.

## Configuration

Add these to the existing protected `.env` file (mode 600, gitignored). Never
commit them and never print them:

```
OPENAI_ENABLED=1
OPENAI_API_KEY=<secret>
OPENAI_ALLOWED_MODELS=gpt-6-luna,gpt-6.1-sol
OPENAI_DEFAULT_MODEL=gpt-6-luna          # first OpenAI entry in the picker; Claude stays the overall default
# optional
OPENAI_REASONING_EFFORT=low              # tool-loop effort; ignored where the model does not support it
OPENAI_MAX_OUTPUT_TOKENS=12000           # tool-loop budget, clamped to 2048..32000
CHAT_OPENAI_TIMEOUT_SECONDS=90
CHAT_OPENAI_MAX_RETRIES=1
```

The key needs these OpenAI project permissions:

- **Responses**: write;
- **Models**: read, used for the availability check.

Nothing else is needed. A restricted project key is recommended.

## Behaviour guarantees

- **Model ids are server-approved.** The picker lists only `/api/models` options.
  - A submission naming an OpenAI model that is not offered gets a 400. Claude never answers it silently.
  - An unknown or retired Claude id still falls back to the default Claude model, as before.
- **A job keeps its provider.** The model is fixed when the question is submitted. Changing the picker later never re-routes a running job.
  - Resending the same idempotency key returns the original job, even if OpenAI has been disabled since. The lookup runs before validation.
- **History is plain text** (`role` / `content`), so a conversation can mix providers.
  - Each assistant message reports the model and provider of the job that wrote it.
  - This comes from a join on `chat_jobs.assistant_message_id`, so no migration is needed.
- **Provider failures end the job as `failed`, with a user-safe message and one of these `error_code` values:**
  - `provider_auth`
  - `provider_rate_limited`
  - `provider_timeout`
  - `provider_unreachable`
  - `model_unavailable`
  - `provider_error`
  - `provider_bad_response`
  - `provider_disabled`

  Logs record the class, HTTP status and request id only. Provider error bodies can echo a masked key, so they are never logged.
- **Each request writes one `chat_timing` log line.** It includes `provider=`, `model=`, `input_tokens`, `output_tokens`, `reasoning_tokens` and per-call latency. It contains no SQL, rows, question text or keys.

## Rollback

There are three ways to back out, from lightest to heaviest:

1. **Configuration only.** Set `OPENAI_ENABLED=0`, or remove the key, and restart laynes-api. The picker returns to the Claude-only list.
2. **Code.** Deploy the previous release commit.
3. **Database.** Nothing to do: there is no schema change.
