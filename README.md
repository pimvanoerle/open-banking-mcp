# open-banking-mcp

An MCP server that gives Claude read-only access to UK bank accounts through
[TrueLayer's Data API](https://docs.truelayer.com/docs/data-api-basics) — one
integration covering Monzo, Starling, HSBC, Barclays, Lloyds, NatWest,
Nationwide, Santander and most other UK banks.

Read-only by design: this server requests Data API scopes only. There is no
code path here that can move money.

> **Status:** working against both the sandbox and a real bank connection.
> Single-user, personal-scale; not hardened for multi-tenant use.

## Install

```bash
git clone https://github.com/pimvanoerle/open-banking-mcp.git
cd open-banking-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

**On an Intel Mac**, that install fails while building `cryptography` unless you
have a Rust toolchain — upstream dropped macOS x86_64 wheels at version 49.
Use the bundled constraints file instead:

```bash
.venv/bin/pip install -e ".[dev]" -c constraints-macos-intel.txt
```

That pin is deliberately not in `pyproject.toml`, so everyone else keeps getting
current `cryptography` security updates.

## Setup

### 1. Create a TrueLayer app

1. Sign up at [console.truelayer.com](https://console.truelayer.com) — free,
   instant, Google/GitHub SSO or email.
2. New accounts start in **sandbox**, which is where you want to begin: a mock
   bank, no rate limits, no real money.
3. Click **Create App +** and choose a client id — 4–30 lowercase letters and
   digits. **It cannot be changed later.** Sandbox ids get a `sandbox-` prefix
   automatically; the live id is the same name without it.
4. Copy the **client secret** — shown once. More can be minted later under
   Settings (up to 6 at a time).
5. Add a **redirect URI** of `http://localhost:8080/callback`. It must match
   byte for byte, scheme and path included — a mismatch is TrueLayer's own
   documented most-common cause of auth failures.

### 2. Configure

Copy `.env.example` to `.env` and fill it in:

```bash
TRUELAYER_CLIENT_ID=sandbox-yourapp
TRUELAYER_CLIENT_SECRET=...
TRUELAYER_REDIRECT_URI=http://localhost:8080/callback
TRUELAYER_ENV=sandbox
```

Load it before running commands:

```bash
set -a; . ./.env; set +a
```

Every `.env*` file is gitignored except `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `TRUELAYER_CLIENT_ID` | — | Required |
| `TRUELAYER_CLIENT_SECRET` | — | Required |
| `TRUELAYER_ENV` | `sandbox` | `sandbox` or `production` |
| `TRUELAYER_REDIRECT_URI` | `http://localhost:8080/callback` | Must be registered in the console |
| `TRUELAYER_SCOPES` | all Data API scopes | Space-separated scope list |
| `TRUELAYER_PROVIDERS` | `uk-cs-mock` / `uk-ob-all uk-oauth-all` | Which banks to offer at consent |
| `TRUELAYER_USE_KEYRING` | `1` | Set `0` to store tokens in a file |
| `TRUELAYER_TOKEN_FILE` | `~/.open-banking-mcp/tokens.json` | Implies file storage |
| `TRUELAYER_CACHE_FILE` | `~/.open-banking-mcp/cache-<env>.db` | Local SQLite cache |
| `TRUELAYER_MAX_AGE_HOURS` | `25` | Age past which cached data is flagged stale |
| `TRUELAYER_HISTORY_DAYS` | `365` | How far back a full sync pulls |
| `TRUELAYER_PSU_IP` | unset | End user's IP; lifts rate limits for user-present calls |

### 3. Connect a bank

```bash
open-banking-mcp auth
```

This opens a browser for consent and stores the tokens in your OS keychain.

In the sandbox, pick the mock bank and log in as `john` / `doe` — `john1`/`doe1`
through `john100`/`doe100` give different account shapes. The sandbox reports
its provider id as `mock`, not `uk-cs-mock`, so that's the name `status` and
`logout` expect.

```bash
open-banking-mcp status                 # connected banks + consent countdown
open-banking-mcp logout ob-santander-personal
```

### 4. Going live

The console has a **Sandbox / Live** toggle. The same app exists in both, but
they are separate environments: **separate client ids, separate secrets,
separate redirect URI registrations.** Register your callback again on the Live
side — it does not carry over.

Keep live credentials in their own file:

```bash
cp .env.example .env.production
# fill in the live client id and secret, set TRUELAYER_ENV=production
set -a; . ./.env.production; set +a
open-banking-mcp auth
```

New secrets can take **up to 15 minutes** to become active. If the first attempt
fails with a 400, wait before assuming something is broken.

Sandbox and production data never mix. Tokens are keyed by environment in the
keychain, the provider index is grouped by environment, and each environment
gets its own cache file. Without that separation, a mock bank's balance could be
reported as real money.

## How it reads data

Every read is served from a **local SQLite cache** by default, refreshed by a
daily sync. This is not just a speed trick: TrueLayer caches responses for an
hour and throttles unattended callers hard unless the request carries an
`X-PSU-IP` header saying a human is present. An agent checking your accounts on
a schedule is exactly the throttled case, so it reads locally instead.

```bash
open-banking-mcp sync     # pull everything; run this daily
open-banking-mcp cache    # what's stored, and when it last synced
```

Every response says how old it is:

```json
{
  "data": { "current": 4049.13, "available": 7049.13, "currency": "GBP" },
  "as_of": "2026-09-13T06:43:52Z",
  "age": "3 hours old",
  "source": "cache",
  "stale": false
}
```

Past `TRUELAYER_MAX_AGE_HOURS` (default 25 — a daily sync plus slack) `stale`
flips to true and a `warning` field is added, so an agent reporting a balance
can say how current it is rather than implying it is live.

**When you need live figures** — you just sent a payment and want to know
whether it landed — every read tool takes `fresh=true`, which bypasses the
cache, queries the bank, and writes the result back:

```
get_balance(account_id="...", fresh=true)
get_transactions(account_id="...", fresh=true)   # also pulls pending
```

`fresh=true` on transactions needs a specific `account_id`: a live refresh is
per-account, not a whole-portfolio sweep, which is precisely the pattern that
trips the rate limiter. It pulls pending transactions too — usually what "did my
payment go through" actually means.

If TrueLayer does throttle you, a sync stops at the first 429 rather than
walking the remaining accounts; continuing would burn the rest of the allowance
and fail anyway. Whatever synced before the limit stays cached.

### Scheduling the daily sync

```
17 6 * * *  cd ~/dev/openbanking-mcp && set -a && . ./.env.production && set +a && .venv/bin/open-banking-mcp sync >> ~/.open-banking-mcp/sync.log 2>&1
```

## MCP tools

| Tool | What it does |
|---|---|
| `list_banks` | Connected banks and last sync time |
| `list_accounts` / `list_cards` | Accounts and cards |
| `get_balance` / `get_card_balance` | One balance |
| `get_balances` | Every balance plus per-currency totals |
| `get_transactions` | Query by date range, account, or text search |
| `list_standing_orders` / `list_direct_debits` | Recurring payments |
| `get_identity` | Account holder details |
| `sync_now` | Refresh the whole cache |
| `cache_status` | What the cache holds |

All except `list_banks`, `sync_now` and `cache_status` accept `fresh`.

### Connecting it to Claude

```json
{
  "mcpServers": {
    "open-banking": {
      "command": "/absolute/path/to/.venv/bin/open-banking-mcp-server",
      "env": {
        "TRUELAYER_CLIENT_ID": "yourapp",
        "TRUELAYER_CLIENT_SECRET": "...",
        "TRUELAYER_ENV": "production"
      }
    }
  }
}
```

## Security

This server holds credentials and a mirror of your financial history. What that
means in practice:

**Read-only.** The requested scopes are `info accounts balance transactions
cards direct_debits standing_orders offline_access` — all Data API. There are no
payment scopes and no payment endpoints in this codebase. The worst case is data
exposure, not money movement.

**On disk**, under `~/.open-banking-mcp/` (directory mode `0700`):

| File | Mode | Contents |
|---|---|---|
| `cache-<env>.db` | `0600` | Balances, transaction history, account numbers, identity |
| `tokens.json` | `0600` | OAuth tokens, only if you opt out of the keychain |
| `providers.json` | `0644` | Provider ids only — no secrets, no financial data |

SQLite journal and WAL side-car files are restricted too; they hold the same
data as the database.

**Tokens** go in the OS keychain by default (service `open-banking-mcp`, account
`<env>:<provider_id>`). The plaintext provider index exists because keyrings
cannot be enumerated. Set `TRUELAYER_TOKEN_FILE` for a `0600` JSON file instead,
on Linux or CI with no keyring daemon; the server falls back to this
automatically when it finds no usable keyring backend.

**Client secrets** belong in `.env` / `.env.production`, never in the repo,
never in a shell history, never pasted into a chat. If one leaks, rotate it in
the console — your bank connection survives, because the refresh token is
already issued.

**Consider where the data ends up.** If you wire this into a chat assistant,
your balances and transactions travel wherever that assistant's messages go.
That's a deliberate decision worth making up front.

**Revoking access** is always available from your banking app or TrueLayer, and
consent expires on its own after 90 days.

## Consent expiry

Open Banking consent lasts **90 days** under FCA rules, then you must
re-authorise in a browser — there is no way around this. `status` shows the
countdown per bank, amber at 21 days and red at 7.

Note that a refresh response often omits `refresh_token` entirely rather than
rotating it. The client carries the existing one forward when that happens;
dropping it would break every subsequent refresh.

## Development

```bash
.venv/bin/pytest            # 63 tests, no network required
```

Tests mock TrueLayer's HTTP endpoints, so the suite runs offline. The
interactive OAuth flow is covered end to end by driving the real localhost
callback server with a fake browser.

## Licence

MIT
