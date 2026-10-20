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

**Usage**

- A visual dashboard mirroring `omp usage`: per-provider cards with progress bars for each rate-limit window (5h / 7d / weekly / monthly), used vs. remaining, live reset countdowns, account/plan info and reset-credit counts; shared windows are deduplicated. Manual refresh + per-provider or global force-refresh (`omp usage invalidate`, whitelist-validated) + optional 60s auto-polling, friendly empty/error states. **One-click "Reset with credit"** on anthropic/openai-codex cards — a confirmed click spends one saved reset credit through the same upstream protocol omp's `/usage reset` uses (server-side reimplementation of `resets.ts`; OAuth tokens are read from `~/.omp/agent/agent.db` and never leave the process).

**Accounts**

- OAuth accounts omp can route per account, grouped by provider (identity from `omp usage accounts`, no tokens; remaining quota from `omp usage`); logins without an identity and API-key credentials are not shown. Each account gets a **priority** and **reserve %** editor that writes omp's native `auth.accountPolicies` in `config.yml` (backup + targeted block rewrite; everything outside the block stays byte-identical); leave both empty to clear. The global default `retry.usageReservePct` is editable on the same page. Policies that match no signed-in account are listed separately with a delete button. Priority only orders healthy, measured accounts and affects new sessions; it never overrides rate limits or reserve. Add accounts with `/login` in omp. A policy block containing comments or flow syntax is shown read-only.
- Each account has a **Disable (fallback only)** / **Enable** button. Disabling is **not a hard off**: it writes the account's reserve as 100% (omp's native policy has no disable field), so the account is skipped normally — sessions pinned to it move away on their next request — and is used only as a fallback when every other account is rate-limited, exhausted, or inside its reserve. Enabling clears the account's reserve (back to the global default; a previous custom reserve is not kept); priority is unchanged.

**Desktop widget** (`widget.py`)

- A borderless dark mini window pinned to the desktop layer (never covers other apps) showing every provider's rate-limit windows from `omp usage --json` — compact bars showing remaining quota (colored by how much is left), remaining %, reset countdown, and a badge for saved reset credits. Runs standalone (no dependency on the web server), refreshes every 60 s, drag anywhere to move (position is remembered; the window is always kept fully inside the nearest monitor's work area, including after resolution/monitor/taskbar changes), right-click for refresh / force-refresh / open web panel / quit. Single-instance guarded; Win11 rounded corners; per-monitor DPI aware; `omp` subprocesses run without console windows.
- Custom provider order: right-click a provider header for **Move up / Move down / Reset order**. The order is saved per machine in `widget-state.json` as `"order": ["openai-codex", "google-antigravity", ...]` (can also be edited by hand while the widget is closed); listed providers come first, unlisted ones keep omp's order after them.

## Requirements

- Python 3.8+ (standard library only, zero dependencies)
- [omp](https://github.com/can1357/oh-my-pi) installed and on `PATH` (used for `omp models --json`)
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

Open [http://127.0.0.1:8788](http://127.0.0.1:8788), click **Switch** on a role, pick a provider group, then a model, **Save**. The change applies immediately — running omp sessions pick it up, no restart needed.

## How it stays safe

`omp config set` rewrites the whole file and strips your comments. This tool never calls it. Instead:

1. **Targeted line edit** — reads `~/.omp/agent/config.yml` and regex-replaces only the target key's line; keys not yet present are inserted at the correct nesting level.
2. **Byte-precise I/O** — `read_bytes`/`write_bytes` with original line-ending detection (LF vs CRLF), so everything except the edited/inserted lines stays byte-identical.
3. **Automatic backup** — every write first copies the config to `config.yml.bak-settings-web-<timestamp>`.
4. **Type validation** — scalar values follow the type reported by `omp config list`. The dedicated fallback API validates keys and selectors, supports block maps/lists and single-line maps, and refuses unsupported complex YAML rather than overwriting it.

Server binds to `127.0.0.1` only. Value/role inputs are validated against a whitelist regex before touching the file.

> ⚠️ Pitfall worth knowing (and the reason for #2): on Windows, Python's `Path.write_text()` silently translates LF to CRLF, which would rewrite your entire config's line endings.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | Current roles (parsed from config.yml) + full model catalog (`omp models --json`) |
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
| `/api/accounts/policy` | POST | `{"provider", "identityKey", "priority", "reservePct"}` — upsert/clear the policy of a signed-in OAuth account; validated, backed up |
| `/api/accounts/policy-delete` | POST | `{"index", "provider", "account"}` — delete one policy; rejected if the entry no longer matches |

## License

[MIT](LICENSE) — not affiliated with the omp project.
