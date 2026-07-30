# External kanban sync (Fizzy, Kanboard, …)

Hermes can mirror a kanban board to a self-hosted external kanban
service, so people file and steer work from a real kanban UI while the
Hermes dispatcher keeps doing what it does today. The local SQLite board
remains the **operational source of truth** — claims, runs, heartbeats,
dependency gating, and notify subscriptions are untouched. A gateway
watcher polls the remote service and keeps the two boards in step:

- Cards filed in the remote UI become Hermes tasks (and get dispatched
  to agent workers like any other task).
- Hermes status changes move cards between remote columns; completions
  close cards; archives park them.
- Comments flow both ways with provenance prefixes.

Two providers ship in-tree:
[Fizzy](https://github.com/basecamp/fizzy), Basecamp's self-hostable
kanban, and [Kanboard](https://kanboard.org), the PHP self-hosted
classic. The integration is a small provider interface
(`hermes_cli/kanban_sync/provider.py`), so other services can be added
by registering another provider.

## Setup (Fizzy)

By default the sync runs in **mirror mode**: `sync init` creates a
dedicated Fizzy board per Hermes board — named
`<board_prefix><Board Name>`, e.g. **Hermes_Default** — with columns
copying the Hermes board exactly. No column mapping, no hand-copied
board ids.

1. In Fizzy, create a personal access token with **Read + Write**
   permission (profile → API → Personal access tokens) and note your
   account id — it's the number in your Fizzy URLs
   (`https://fizzy.example.com/897362094/...`).

2. Configure the provider credentials in `~/.hermes/config.yaml`:

   ```yaml
   kanban:
     sync:
       fizzy:
         base_url: "https://fizzy.example.com"
         account_slug: "897362094"
         token_env: HERMES_FIZZY_TOKEN   # or token: "..." inline
   ```

   Export the token in the gateway's environment:
   `export HERMES_FIZZY_TOKEN=...`

3. Bootstrap:

   ```
   hermes kanban sync init
   ```

   This verifies auth, creates (or reuses, if a board with that exact
   name already exists) the **Hermes_Default** board, creates its
   columns, then writes the pairing and `enabled: true` back to
   config.yaml for you. Use `--board <slug>` to mirror a non-default
   board; set `kanban.sync.board_prefix` to change the `Hermes_` prefix
   (`""` = no prefix).

   To pair a **pre-existing** remote board instead, pass
   `--remote-board <id>` — then nothing is auto-written and the command
   prints the config snippet to add yourself (see mapped mode below for
   the classic layout).

4. Either restart the gateway (the sync watcher starts automatically
   when `kanban.sync.enabled` is true) or drive it manually:

   ```
   hermes kanban sync once        # one pass
   hermes kanban sync once --full # ignore the cursor, full rescan
   hermes kanban sync status      # pairings, cursors, link counts, errors
   ```

`HERMES_KANBAN_SYNC=0` in the gateway env disables the watcher without
editing config (same escape-hatch pattern as
`HERMES_KANBAN_DISPATCH_IN_GATEWAY`).

## Setup (Kanboard)

Kanboard talks JSON-RPC on a single endpoint; the provider only needs
the instance root URL and a token.

1. Grab a token. The simplest is the **application API token** from
   *Settings → API* (it bypasses per-project permissions). To attribute
   sync activity to a real account instead, use that user's **personal
   API token** (*user profile → API*) and set `username` to their
   login; sync comments are then posted as that user.

2. Configure `~/.hermes/config.yaml` and pick the provider:

   ```yaml
   kanban:
     sync:
       provider: kanboard
       kanboard:
         base_url: "https://kanboard.example.com"
         token_env: HERMES_KANBOARD_TOKEN   # or token: "..." inline
         # username: alice                  # personal-token auth only
   ```

   Export the token in the gateway's environment:
   `export HERMES_KANBOARD_TOKEN=...`

3. Bootstrap and run exactly as with Fizzy: `hermes kanban sync init`,
   then restart the gateway or drive passes with
   `hermes kanban sync once`.

### Kanboard notes

- **Mirror init creates a clean project**: Kanboard seeds new projects
  with default columns (Backlog / Ready / Work in progress / Done); the
  provider removes them so the mirrored Hermes columns are the whole
  board. `done` reuses Kanboard's native **close** state — completed
  tasks disappear from the board (Kanboard's normal workflow), and
  closing a task in the Kanboard UI completes it in Hermes.
- **No inbox, no archive**: in mapped mode `triage` lands in the
  project's **first column** (there is no untriaged state), and
  `archived` **closes** the task — on the next pull that reads back as
  `done`, once, then settles. In mirror mode both statuses get real
  columns and neither quirk applies.
- **Tags are not imported** (Kanboard task listings omit them), so the
  `assignee:<profile>` tag mapping is unavailable — imports fall back
  to `kanban.sync.default_assignee`. Nothing marks cards *golden*.
- **Comment attribution**: Kanboard requires a posting user id. With
  the application token, sync comments are posted as the built-in
  admin (user id 1); with `username` auth, as that user. Either way
  the `[hermes:<author>]` prefix carries the real provenance.
- **Auth lockout**: three failed authentications lock the account
  until it's unlocked via the web login form. A misconfigured token
  surfaces as an auth error and the watcher backs off rather than
  retrying — fix the credentials before restarting.

## Status ↔ location mapping

### Mirror mode (default, `kanban.sync.mode: mirror`)

Every Hermes column appears on the remote board under its own name. A
provider built-in whose name matches a Hermes column is reused instead
of duplicated — on Fizzy that's the closed state ("Done") — and
built-ins matching no Hermes column ("Maybe?", "Not Now") are ignored
entirely:

| Hermes status | Fizzy location |
|---|---|
| `triage` | column **Triage** |
| `todo` | column **Todo** |
| `scheduled` | column **Scheduled** |
| `ready` | column **Ready** |
| `running` | column **Running** |
| `blocked` | column **Blocked** |
| `review` | column **Review** |
| `done` | closed ("Done" — Fizzy's built-in, reused by name match) |
| `archived` | column **Archived** |

Consequences of ignoring the built-ins:

- Cards sitting in the **"Maybe?" inbox are invisible to sync** — they
  are not imported until a human drags them into a mirrored column
  (start with **Triage**). Closing a card still maps to `done` in both
  directions.
- Cards in **"Not Now" are left alone** (no local status opinion), and
  Hermes never parks cards there — `archived` tasks go to the real
  **Archived** column. A card someone moved to "Not Now" snaps back to
  a mirrored column on the task's next local status change.

`column_map` is ignored in mirror mode.

### Mapped mode (`kanban.sync.mode: mapped`)

The pre-mirror layout: pair an existing remote board
(`sync init --remote-board <id>` is required) and lay Hermes statuses
onto its columns via `kanban.sync.column_map`. The engine auto-creates
missing columns:

| Hermes status | Fizzy location |
|---|---|
| `triage` | untriaged inbox ("Maybe?") |
| `todo` | column **Todo** |
| `ready` | column **Ready** |
| `running` | column **In Progress** |
| `review` | column **Review** |
| `blocked`, `scheduled` | column **Blocked** |
| `done` | closed ("Done") |
| `archived` | "Not Now" |

### Both modes

Remote → local moves use the structured verbs where possible: closing a
card completes the task (`complete_task`, so run history stays correct),
dragging to **Blocked** blocks it with `kind=needs_input`, dragging a
blocked card to **Ready** unblocks it. Transitions Hermes refuses — e.g.
promoting a dependency-gated child to Ready before its parents finish —
are pushed back: the card snaps to the column matching local truth on
the same sync pass.

Dragging a card into a column outside the mode's map leaves the local
status untouched (the bridge has no opinion about custom columns).

## Intake and export

- **Intake** (`kanban.sync.intake`): which remote cards become tasks.
  `mode: all` (default) imports every published, non-closed,
  non-"Not Now" card on the paired board; `mode: columns` limits intake
  to the listed column names (already-linked cards keep syncing).
  Drafted cards are never imported.
- **Assignee**: an `assignee:<profile>` tag on the card wins, then
  `kanban.sync.default_assignee`, then `kanban.default_assignee`.
- **Priority**: cards marked *golden* import with
  `kanban.sync.golden_priority` (default 2).
- **Export** (`kanban.sync.export`): tasks created locally (CLI,
  dashboard, agents) are exported as cards. `backfill: false` (default)
  only exports tasks created after the pairing existed.
- Synced task bodies carry a trailing `[<provider>] <card url>` line
  (e.g. `[fizzy] …`, `[kanboard] …`) linking to the counterpart card.

## Comments

Comments sync both ways with provenance prefixes:

- A local comment by `techlead` appears on the card as
  `[hermes:techlead] …` (providers attribute every API comment to the
  API user, so the prefix is the only reliable authorship signal).
- A card comment by `Dana` appears on the task thread with author
  `<provider>:Dana` (e.g. `fizzy:Dana`, `kanboard:Dana`).

The engine keeps a ledger of comment refs it created or imported, so
comments never ping-pong or duplicate even across cursor resets.

## Conflict policy

Each sync pass compares both sides against per-link fingerprints of
their last-synced state:

- Only one side changed → that side wins (normal propagation).
- **Both changed → the remote wins** (humans on the board are the
  primary workflow), *except* the local status when the task recorded a
  terminal worker outcome (`completed` / `blocked` / `gave_up`) since
  the last sync — worker results are never silently reverted. Title and
  body still take the remote edit.
- Every conflict lands a `sync_conflict` event on the task for audit
  (`hermes kanban log <task-id>`).

Deletions: a card deleted remotely blocks the linked task
(`needs_input`) and unlinks it; a task deleted locally leaves a farewell
comment on the card and unlinks it.

## Operational notes

- **Mirror board naming**: the board name is only computed at `sync
  init`. Renaming the Hermes board (or the remote board) later doesn't
  re-pair anything — but re-running `sync init` after a rename will
  create a *second* remote board under the new name. Reuse-by-name also
  means two local boards with identical display names need distinct
  `board_prefix` values before each can get its own mirror.
- **Fizzy auto-postpone**: Fizzy boards move cards inactive for
  `auto_postpone_period_in_days` (default 30) into "Not Now". In mirror
  mode the sync ignores "Not Now", so long-idle cards drift off the
  mirrored columns without changing the Hermes status; the card comes
  back on the task's next local status change.
- **Polling**: the watcher polls with `interval_seconds` (default 30s),
  using the provider's activity cursor (`last_active_at` in Fizzy) and
  fingerprint no-ops, so idle boards cost one listing request per tick.
  Every `full_resync_every` polls (default 60) it runs a full rescan to
  catch deletes and anything a cursor pull can miss. Webhooks are a
  possible future optimization; the polling engine is deliberately
  webhook-agnostic (a receiver would just enqueue an immediate pull).
- **Multi-gateway**: only one gateway machine-wide may run the sync — a
  `.sync.lock` advisory lock (same backstop as the dispatcher's
  `.dispatcher.lock`) refuses a second concurrent syncer, which would
  otherwise fight over cursors. A per-pairing lock additionally stops a
  manual `hermes kanban sync once` from interleaving with a watcher tick
  on the same pairing (the CLI reports "busy" and exits non-zero).
- **Failure behaviour**: per-pairing errors are logged and retried with
  jittered backoff (auth failures warn at most once per 5 minutes;
  `Retry-After` on 429s is honored). One bad card never aborts a pass —
  it's recorded in `sync status` / stats errors.
- **Trust boundary**: remote card content is untrusted input. Imported
  comments are attributed to `<provider>:<name>` authors, and the sync
  never grants remote content any authority beyond ordinary task text.
