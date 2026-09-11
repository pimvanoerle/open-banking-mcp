# open-banking-mcp

An MCP server that gives Claude read-only access to UK bank accounts through
[TrueLayer's Data API](https://docs.truelayer.com/docs/data-api-basics) — one
integration covering Monzo, Starling, HSBC, Barclays, Lloyds, NatWest,
Nationwide, Santander and most other UK banks.

Read-only by design: this server requests Data API scopes only. There is no
code path here that can move money.

> **Status:** early. The OAuth layer and CLI work; the MCP tools are next.

## Setup

### 1. Create a TrueLayer console account

1. Sign up at [console.truelayer.com](https://console.truelayer.com) — free,
   instant, Google/GitHub SSO or email.
2. New accounts start in the **sandbox** environment, which is what you want
   first. Sandbox has a mock bank, no rate limits and no real money.
3. Click **Create App +** and give it a client id: 4–30 lowercase letters and
   digits, no special characters. **It cannot be changed later.** Sandbox apps
   get a `sandbox-` prefix automatically.
4. Copy the **client secret** from the credentials screen — it is shown once.
   You can mint more later under the app's Settings page.
5. Under the app's settings, add a **redirect URI** of
   `http://localhost:8080/callback`. This must match byte for byte what the
   server sends, including the scheme and path; a mismatch is the single most
   common cause of auth failures.

### 2. Configure

```bash
export TRUELAYER_CLIENT_ID=sandbox-yourapp
export TRUELAYER_CLIENT_SECRET=...
export TRUELAYER_REDIRECT_URI=http://localhost:8080/callback
export TRUELAYER_ENV=sandbox          # or "production"
```

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `TRUELAYER_SCOPES` | all Data API scopes | Space-separated scope list |
| `TRUELAYER_PROVIDERS` | `uk-cs-mock` (sandbox) | Which banks to offer at consent |
| `TRUELAYER_USE_KEYRING` | `1` | Set `0` to store tokens in a file |
| `TRUELAYER_TOKEN_FILE` | `~/.open-banking-mcp/tokens.json` | Implies file storage |

### 3. Connect a bank

```bash
open-banking-mcp auth
```

This opens a browser, you pick a bank and consent, and the tokens land in your
macOS Keychain (or a `0600` JSON file on systems without a keyring).

In the sandbox, choose the mock bank and log in with username `john`, password
`doe` — `john1`/`doe1` through `john100`/`doe100` also work for testing
different account shapes. Note that the sandbox reports its provider id as
`mock`, not `uk-cs-mock`, so that is the name `status` and `logout` expect.

```bash
open-banking-mcp status          # connected banks + consent countdown
open-banking-mcp logout uk-ob-monzo
```

## Consent expiry

Open Banking consent lasts **90 days** under FCA rules, then you must
re-authorise in a browser — there is no way around this. `status` shows the
countdown per bank and colours it amber at 21 days and red at 7.

## Token storage

Tokens go in the OS keychain by default (service name `open-banking-mcp`, one
entry per bank). A plaintext list of *which* banks are connected — no secrets —
lives at `~/.open-banking-mcp/providers.json`, because keyrings can't be
enumerated.

Set `TRUELAYER_TOKEN_FILE` to use a `0600` JSON file instead, on Linux or CI
where no keyring daemon is running. The server falls back to this automatically
if it finds no usable keyring backend.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Licence

MIT
