# omp Settings Web (omp-settings-web)

**[English](README.md) | [中文](README_CN.md)**

## Screenshots

**Desktop widget** — desktop-level panel (stays beneath app windows) showing remaining quota per provider:

![desktop widget](docs/widget.png)

**Usage dashboard** — per-provider quota cards with force-refresh and reset-credit redemption:

![usage dashboard](docs/en/usage.png)

**Model roles** — beginner notes per role, two-level picker to switch models:

![model roles](docs/en/roles.png)

**All settings** — 529 keys in a categorized sidebar, each with an explanation:

![all settings](docs/en/settings.png)

**Model visibility** — choose which models appear in the `/model` picker:

![model visibility](docs/en/models.png)

## Features

**Model roles**

- Role matrix with two-level picker: provider group → model; auto-positions on the current value; `@role` aliases kept as a fallback entry.
- Each role shows a beginner-friendly explanation below it (which features use the role, what model to assign, and the fallback behavior when unset); a general note at the top explains the role mechanism (@aliases, *, comma candidates, custom roles).
- Ctrl+P cycle order editor (`cycleOrder`): move up/down, remove, add existing roles or a custom role name; roles without a model can be bound to one right there (written to `modelRoles`). Save / cancel / restore default — restoring removes `cycleOrder` so omp falls back to `smol → default → slow`. Empty lists, duplicates, `@aliases` and invalid characters are rejected.
- Failure fallback editor: ordered backup models for roles and existing model/provider chains, with add, remove, move up/down and cancel. Distinguishes unset (remove the key and use omp's normal rules), explicitly disabled (`[]`), and a custom chain. Existing hidden/offline entries are retained; new entries use explicit `provider/model-id` selectors, not `@role` aliases.
- Fallback entries and concrete model keys support model version tags (for example, `ollama-cloud/deepseek-v4.1-flash:1017`) and a thinking suffix after the tag (`:1017:high`); version tags are not rejected as invalid thinking levels.
- Controls for `retry.modelFallback`, `retry.fallbackOn` and `retry.fallbackRevertPolicy`. Exact model keys take precedence over provider wildcards, then the active role and `default`; model-kind roles use their own chains rather than the chat `default`.

**All settings**

- Full catalog from `omp config list --json` (500+ keys) with current values, types, and descriptions.
- All 529 keys carry detailed beginner-oriented Chinese names & explanations — what each value does, the default, when it is worth changing, and related settings (upstream English text is available on hover).
- Scalar settings are editable inline; arrays and nested blocks are read-only here. Use the dedicated editor on Model Roles for `retry.fallbackChains`.
- Sidebar category navigation: ~35 semantic categories sorted by size; search shows per-category hit counts and dims empty ones.
- Search + "only customized" filter; badges distinguish keys you have set from defaults.

**Model visibility**

- Choose which models appear in omp's `/model` picker, Ctrl+P cycling and this page's role dropdowns — per model or per provider, with search. Hidden models are **not disabled**: roles already assigned to them keep working. Writes `enabledModels` in `config.yml` (backed up, live-reloaded by omp); fully selected providers collapse to `provider/*`, the default role's model is pinned first and always shown, and patterns matching no current model (offline providers) are preserved.
- Every provider group header in **Model visibility** carries a **Delete provider** button when that provider is defined under `providers` in the local `models.yml` (or `models.yaml`). Providers defined there but absent from omp's catalog (empty providers) still get a group so they can be removed; blocks holding only `modelOverrides` are built-in overrides and show a tag instead of a button. The API returns names, model ids and the config references that would be cleared — never `baseUrl`, `apiKey` or `headers`.
- **Delete provider** removes that block (deeper-indented comments inside it go with it; comments at the providers' key indent belong to the next provider and stay) and clears every reference to it **in the same transaction**, but only inside the config fields omp actually treats as model selectors: `modelRoles.*`, `retry.fallbackChains` (both keys and values), `enabledModels` (block list and flow form), `task.agentModelOverrides.*`, `modelPresets.*.modelRoles.*`, `sharpshooter.model` and `edit.modelVariants.*`. Every other field — paths, URLs, `bash.patterns`, arbitrary nested flow config — is left byte-identical and never disables deletion. Only **block-style** config is rewritten: scalar selector values (including comma lists) under `modelRoles`, `modelPresets.*.modelRoles` and `task.agentModelOverrides`; block keys and block list items under `retry.fallbackChains`; and block list items under `enabledModels`. Within such a value only the candidates that belong to that provider are dropped; a role or chain left with nothing is removed, and `modelRoles`/`fallbackChains`/`agentModelOverrides`/`modelPresets` collapse to `{}` while `enabledModels` collapses to `[]`. Any **flow container** (`{…}` / `[…]`) is never rewritten. Inside the model-related top-level blocks (`modelRoles`, `retry`, `task`, `modelPresets`, `enabledModels`, `sharpshooter`, `edit`) and at any depth, a line whose value starts with `{` or `[` is refused as `unsupported` whenever its text mentions this provider name or one of its declared model ids on a word boundary — a deliberately conservative text check that may over-block but never under-blocks. **Multi-line** flow containers are tracked as regions — and a region can only start on a line whose *value* (after `key:` or a list item's `-`) begins with `{` or `[`, so brackets inside plain strings (`match: echo [`) or block-scalar bodies (`description: |`) never swallow the lines that follow: from the line that opens the `{`/`[` until it closes (quote- and comment-aware), the whole span is one flow value, so no line inside it ever becomes a rewrite operation, and if any line in the span mentions the provider or a declared model id the deletion is refused with the container's own path in the reason. The reason names the location and asks for block style. Flow that mentions neither is left alone and does not block. A bare model id is cleared only when this provider is its *only* source in omp's **full** catalog (`omp models --kind all`, so image/tts/stt/judge/search models count as providers too — omp resolves duplicate ids with `pickPreferredModel()`), so shared ids keep working. When a bare id would need that judgement but the catalog cannot be read (omp missing, non-zero exit, unparsable output or an empty list), deletion is refused with `unavailable` and the button is disabled — the page never guesses an owner and writes nothing. Trailing comments, indentation and line endings of rewritten lines are preserved; only the value span is replaced. References are never a reason to refuse; the confirmation dialog lists every location with its current and resulting value, and warns when the visibility list would end up empty (an empty list makes omp show every model). A selector shape that cannot be rewritten byte-exactly is reported as `unsupported` and the button is disabled with that reason. Both files are backed up to `<name>.bak-settings-web-<stamp>` **before** anything is replaced; a failure restores from those backups, and if restoration itself fails the response is an honest partial (HTTP 500) naming the unrestored files and the backup paths to restore by hand. The request carries the SHA-256 of `models.yml` **and** of the `config.yml` the dialog was built from, so a view that no longer matches disk is rejected (stale) instead of silently clearing references the user never saw.
- Each model has a **Test** button: reuse omp's signed-in credentials for one real short `omp bench` request to that exact model (thinking requested off, 256-token output limit, no fallback chain). Shows success/failure, total time, first-token and model-request timing, with expandable failure details. One test at a time, with a combined 60-second catalog/request deadline; no configuration or checkbox changes and no automatic retries. Consumes a small amount of quota and checks only this short text request, not tools, images or long-context support. Results stay in page memory and disappear on reload.

**Usage**

- A visual dashboard mirroring `omp usage`: per-provider cards with progress bars for each rate-limit window (5h / 7d / weekly / monthly), used vs. remaining, live reset countdowns, account/plan info and reset-credit counts; shared windows are deduplicated. Manual refresh + per-provider or global force-refresh (`omp usage invalidate`, whitelist-validated) + optional 60s auto-polling, friendly empty/error states. **One-click "Reset with credit"** on anthropic/openai-codex cards — a confirmed click spends one saved reset credit through the same upstream protocol omp's `/usage reset` uses (server-side reimplementation of `resets.ts`; OAuth tokens are read from `~/.omp/agent/agent.db` and never leave the process).

**Accounts**

- Signed-in OAuth accounts grouped by provider (identity from `omp usage accounts`, no tokens; remaining quota from `omp usage`; removable entries come from omp's `get_logout_accounts` RPC). Both OAuth logins and API-key credentials are listed and removable; a credential omp gives no identity (e.g. devin) cannot take a policy but can still be logged out from its row. Each account gets a **priority**, **reserve %** and **reclaim above %** editor (`reclaimAbovePct`, omp ≥18.12.4: sessions demoted to a lower-priority account return once its remaining share recovers above that threshold; it must exceed the reserve) that writes omp's native `auth.accountPolicies` in `config.yml` (backup + targeted block rewrite; everything outside the block stays byte-identical); leave all three empty to clear. The global default `retry.usageReservePct` is editable on the same page. Policies that match no signed-in account are listed separately with a delete button. Priority only orders healthy, measured accounts and affects new sessions; it never overrides rate limits or reserve. Add accounts with `/login` in omp. A policy block containing comments or flow syntax is shown read-only.
- Each row has a **Log out** button. The row and the confirmation both show the label/detail and `#credentialId`
  that omp itself reports, so two logins with the same email in different organizations are distinguishable.
  Confirming calls omp's own `logout` RPC (the same path as `/logout`): one stored credential, OAuth or API key,
  removed by that exact id. No SQLite writes, no cancellation of the paid plan, other accounts untouched, and
  `modelRoles` / `models` / account policies left alone. Before deleting, the server verifies that the id is still
  listed by omp, that the local credential store agrees on the provider, and that the identity is **exactly equal**
  after normalization (a field appearing or disappearing counts as a change, so an old page cannot remove a login that
  omp since upgraded to an organization-scoped identity); a credential whose owner cannot be verified is refused rather
  than guessed. Failures keep the row and surface the error; success reloads the list and reports only whether another
  auth source remains. Removal is disabled, with an explanation, when omp keeps credentials in an auth-broker
  (deleting would act on a shared remote store) or when the agent directory cannot be determined.
- Each account has a **Disable (fallback only)** / **Enable** button. Disabling is **not a hard off**: it writes the account's reserve as 100% (omp's native policy has no disable field), so the account is skipped normally — sessions pinned to it move away on their next request — and is used only as a fallback when every other account is rate-limited, exhausted, or inside its reserve. Enabling clears the account's reserve (back to the global default; a previous custom reserve is not kept); priority is unchanged.

**Desktop widget** (`widget.py`)

- A borderless dark mini window pinned to the desktop layer (never covers other apps) showing every provider's rate-limit windows from `omp usage --json` — compact bars showing remaining quota (colored by how much is left), remaining %, reset countdown, and a badge for saved reset credits. Runs standalone (no dependency on the web server), refreshes every 60 s, drag anywhere to move (position is remembered; the window is always kept fully inside the nearest monitor's work area, including after resolution/monitor/taskbar changes), right-click for refresh / force-refresh / open web panel / quit. Single-instance guarded; Win11 rounded corners; per-monitor DPI aware; `omp` subprocesses run without console windows.
- Custom provider order: right-click a provider header for **Move up / Move down / Reset order**. The order is saved per machine in `widget-state.json` as `"order": ["openai-codex", "google-antigravity", ...]` (can also be edited by hand while the widget is closed); listed providers come first, unlisted ones keep omp's order after them.
- Accounts disabled as "fallback only" in the web panel (policy `reservePct: 100`, read via `omp config get auth.accountPolicies` on each refresh) get a dimmed header with a 停用·兜底 tag; omp may still use them as a fallback, so their quota is shown as usual. The account email is shown next to the provider name; rows and providers without a numeric remaining share are hidden.
- The quota list itself is `QuotaView` in `widget.py` (data + drawing, embeddable in any Tk parent); the standalone window is only a shell around it. The Model Quota tab of [desktop-widgets](https://github.com/libanxian003/desktop-widgets) loads this file and reuses it, so quota changes are made here once.

## Requirements

- Python 3.8+ (standard library only, zero dependencies)
- [omp](https://github.com/can1357/oh-my-pi) installed and on `PATH` (`omp models --json` for the catalog; connectivity testing requires a version supporting `omp bench --json`)
- **Windows** for the desktop widget (`widget.py`): it pins itself to the desktop layer, keeps Win11 rounded corners and is per-monitor DPI aware through Win32 calls, none of which exist elsewhere. The web panel (`server.py`) is plain Python and runs on any OS.

## Usage

```bash
python server.py            # starts http://127.0.0.1:8788 and opens your browser
python server.py --no-browser
```

Desktop widget (Windows):

```bash
pythonw widget.py           # no console window; right-click the widget for the menu
```

Isolated profile / port (for testing against a throwaway omp profile):

```bash
OMP_PROFILE=my-test OMP_SETTINGS_PORT=8799 python server.py --no-browser
```

The panel does not inject `--profile`: every `omp` call inherits your environment and omp resolves
the profile itself (`--profile` > `OMP_PROFILE` > `PI_PROFILE`, where an empty value or `default`
means the default profile). The credential/config directories are then taken from `omp config path`,
so a test run never touches your real credentials or config. Unset by default (`~/.omp/agent`, port 8788).

Open [http://127.0.0.1:8788](http://127.0.0.1:8788), click **Switch** on a role, pick a provider group, then a model, **Save**. The change applies immediately — running omp sessions pick it up, no restart needed.

## How it stays safe

`omp config set` rewrites the whole file and strips your comments. This tool never calls it. Instead:

1. **Targeted line edit** — reads `~/.omp/agent/config.yml` and regex-replaces only the target key's line; keys not yet present are inserted at the correct nesting level.
2. **Byte-precise I/O** — `read_bytes`/`write_bytes` with original line-ending detection (LF vs CRLF), so everything except the edited/inserted lines stays byte-identical.
3. **Automatic backup** — every write first copies the config to `config.yml.bak-settings-web-<timestamp>`.
4. **Type validation** — scalar values follow the type reported by `omp config list`. The dedicated fallback API validates keys and selectors, supports block maps/lists and single-line maps, and refuses unsupported complex YAML rather than overwriting it.

Server binds to `127.0.0.1` only. Value/role inputs are validated against a whitelist regex before touching the file.

Credential removal never edits `agent.db`. The panel only reads `provider` and `identity_key` from
`auth_credentials` (never `data`) to cross-check identity, and the deletion itself is omp's own
`logout` RPC, which also refreshes the model registry and account pins. Every RPC child process is
killed after the call (25s cap) and its stderr is discarded, so no credential text can reach a log.

Destructive endpoints (`/api/accounts/logout`) reject cross-site `Origin`/`Referer` before reading
the body, so another website open in your browser cannot borrow localhost access to delete credentials.

> ⚠️ Pitfall worth knowing (and the reason for #2): on Windows, Python's `Path.write_text()` silently translates LF to CRLF, which would rewrite your entire config's line endings.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | Current roles (parsed from config.yml) + full model catalog (`omp models --json`) |
| `/api/models/test` | POST | `{"selector":"provider/model-id"}` — one real request; returns `{ok, selector, code, elapsedMs, model?, durationMs?, ttftMs?, error?}`. Exact current catalog entries only; 400 for invalid model, 409 when another test is running, 200 for other test outcomes; errors are redacted. |
| `/api/set` | POST | `{"role": "...", "value": "..."}` — backup + targeted write |
| `/api/settings` | GET | Full settings catalog (`omp config list --json`) + zh annotations + `__groupmap` |
| `/api/set-key` | POST | `{"key": "...", "value": "..."}` — type-coerced scalar write, backup + targeted line edit |
| `/api/fallback` | GET | Fallback chains and `modelFallback` / `fallbackOn` / `fallbackRevertPolicy` |
| `/api/fallback` | POST | `{"key":"default","chain":["provider/model"]}`; `[]` disables fallback, `null` removes the key. Updates only that chain, with a backup. |
| `/api/cycle-order` | GET | Ctrl+P cycle order `{order, custom, default}`; omp's default order when unset |
| `/api/cycle-order` | POST | `{"order":["smol","default","e"]}` writes it; `{"order": null}` removes the key to restore the default; backed up |
| `/api/usage` | GET | `omp usage --json` passthrough — per-provider quota/usage reports |
| `/api/usage/invalidate` | POST | `{"provider": "all"|<provider id>}` — whitelist-checked `omp usage invalidate`, forces re-fetch |
| `/api/usage/redeem` | POST | `{"provider": "anthropic"\|"openai-codex"}` — spends one saved reset credit via the same upstream calls as omp's `/usage reset` (two-step request_id confirm for Anthropic, single idempotent consume for Codex); structured `{ok, code, error}` responses, tokens never exposed |
| `/api/accounts` | GET | Accounts per provider (identity, quota, matching policy), unmatched policies, default reserve |
| `/api/accounts/policy` | POST | `{"provider", "identityKey", "priority", "reservePct", "reclaimAbovePct"}` — upsert/clear the policy of a signed-in OAuth account; validated, backed up |
| `/api/accounts/policy-delete` | POST | `{"index", "provider", "account"}` — delete one policy; rejected if the entry no longer matches |
| `/api/accounts/logout` | POST | `{"provider", "credentialId", "identityKey"?}` — remove one stored credential (OAuth or API key) by omp's `credentialId`; soft delete, the subscription is untouched. Requires same-origin `Host` plus a matching `Origin`/`Referer`; refuses when the credential is gone/changed (409), the identity does not match exactly (409), the provider is unverifiable or omp runs on an auth-broker (503/409), or the RPC is unavailable (503) |

## License

[MIT](LICENSE) — not affiliated with the omp project.
