### AI Base: Multi-Provider AI Connector

Technical name: `ki_erp_ai_base` · Frappe / ERPNext v16 · by kalvyai

Shared AI layer that any Frappe app can build on: providers, API keys, generation settings, token
usage and monthly caps. Has no business features of its own; KalvyAI apps, integrators and in-house
developers add those. Same features and API as the Odoo module `ki_ai_base`.

Needs Frappe only. When ERPNext is installed, providers and usage get a **Company** field.

### Providers

| Type | Default endpoint | Reads PDFs | Reads images |
|---|---|---|---|
| OpenAI | api.openai.com/v1 (Responses API) | yes | yes |
| Anthropic Claude | api.anthropic.com/v1 (Messages) | yes | yes |
| Google Gemini | generativelanguage.googleapis.com/v1beta | yes | yes |
| xAI Grok | api.x.ai/v1 (chat completions) | no | yes |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/v1` (set Base URL; model = deployment) | no | yes |
| Mistral AI | api.mistral.ai/v1 | no | yes |
| Cohere | api.cohere.com/v2 | no | yes |
| Perplexity | api.perplexity.ai | no | yes |
| NVIDIA NIM | integrate.api.nvidia.com/v1 (chat completions) | no | yes, with a vision model |

Other OpenAI-compatible servers (Ollama, vLLM, self-hosted NIM) can be reached by picking a
chat-completions type and setting its Base URL.

### Per provider (KI AI Provider)

- **API Key**, stored encrypted and visible to *AI Manager* and *System Manager* only.
- **Model**, picked from a dropdown. **Fetch Models** fills it from the provider's models API with
  the configured key; a model not listed can be typed in.
- An optional **Base URL** (proxy or gateway).
- **Priority**: apps try providers from the lowest number up.
- **Temperature** (0–2), with *Send Temperature* off for models that reject it (off by default for
  OpenAI).
- **Max Output Tokens per Call**.
- **Reasoning Effort** (Low, Medium, High; empty = provider default) for reasoning models. Not
  available for Cohere.
- **Timeout per Call** (default 70 s), retries of brief overloads included. Keep it below the web
  worker timeout (`http_timeout`, 120 s by default); run longer work in a background job.
- **Monthly Token Cap** (input + output, 0 = unlimited). Once reached, calls are refused until the
  next month.
- **Company** (with ERPNext): empty shares the provider with every company. User Permissions on
  Company apply.
- **Test Connection** sends a one-word prompt with the real key and model.
- The month's input, output and total tokens, the share of the cap used, and the number of calls.

### Usage log (KI AI Usage Log)

*AI Base > Usage* lists every call with its provider, model, user, feature, linked document, input
and output tokens, duration and status. Use the Report view for totals and group-by.

Failed calls are logged too. Tokens billed for an answer that was then rejected (refusal, invalid
JSON, cut off at the output-token limit) are counted. When the request or job that made a call fails
and Frappe rolls it back, its usage rows are written again by a background job, so the cap stays
right.

### Roles

- **AI User**: can see providers (without their API key), models and usage.
- **AI Manager**: can add providers, enter API keys, and set temperature, token limits and caps.
- **System Manager** has the same rights as AI Manager.

### Using it from another app

Add the dependency to the app's `hooks.py`:

```python
required_apps = ["ki_erp_ai_base"]
```

Then call the providers in priority order, moving to the next when one is busy, over its cap or
cannot read the files:

```python
from ki_erp_ai_base.api import AIError, generate

try:
    result, usage = generate(
        "You classify support tickets.",          # instructions
        text=ticket.description,                  # user content
        files=[file_doc.get_content()],           # optional PDFs / images (bytes)
        json_schema={"type": "object", "properties": {...}},  # optional
        feature="ticket_triage",                  # tag in the usage log
        reference=ticket,                         # document linked in the usage log
        company=ticket.company,                   # optional: that company's providers
    )
except AIError as error:
    frappe.throw(str(error))
```

- `result` is the parsed JSON when `json_schema` is given, otherwise text.
- `usage` is `{"tokens_in", "tokens_out", "usage_log", "provider"}`.
- To use one provider (e.g. chosen in your app's settings):
  `frappe.get_doc("KI AI Provider", name).generate(...)`, same arguments without `company`.
- `get_providers(company=None)` returns the enabled providers in priority order;
  `provider.can_read(mimetype)` tells whether one accepts a file type.
- `timeout=` (seconds) shortens the provider's call timeout for this call.
- Errors, all subclasses of `frappe.ValidationError`, raised without a message dialog so callers can
  catch them:
  - `ProviderBusy`: temporary failure (timeout, rate limit, outage), retry later.
  - `BudgetExceeded`: the provider's monthly cap is spent.
  - `FileNotSupported`: the provider cannot read one of the files; nothing was sent.
  - `AIError`: anything else (bad key, refused, invalid or truncated output).
- Permissions are not checked by `generate`: the calling app decides who may use AI.
- A usage row written inside a savepoint that is rolled back
  (`frappe.db.rollback(save_point=...)`) is lost.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Kalvyai-erpnext/ki_erp_ai_base --branch version-16
bench install-app ki_erp_ai_base
```

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade). Tests mock every provider API, no
network:

```bash
bench --site <site> set-config allow_tests true
bench --site <site> run-tests --app ki_erp_ai_base
```

### License

MIT. Free and open source. Support: support@kalvyai.com or GitHub issues.
