# External kanban sync (Fizzy, …)

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

The first supported provider is [Fizzy](https://github.com/basecamp/fizzy),
Basecamp's self-hostable kanban. The integration is a small provider
interface (`hermes_cli/kanban_sync/provider.py`), so other services can
be added by registering another provider.

## Setup (Fizzy)

1. In Fizzy, create a personal access token with **Read + Write**
   permission (profile → API → Personal access tokens) and note your
   account id — it's the number in your Fizzy URLs
   (`https://fizzy.example.com/897362094/...`).

2. Configure `~/.hermes/config.yaml`:

   ```yaml
   kanban:
     sync:
       enabled: true
       provider: fizzy
       interval_seconds: 30
       pairings:
         - board: ""             # local board slug ("" = default board)
           remote_board: "abc123" # Fizzy board id (from the board URL)
       fizzy:
         base_url: "https://fizzy.example.com"
         account_slug: "897362094"
         token_env: HERMES_FIZZY_TOKEN   # or token: "..." inline
   ```

   Export the token in the gateway's environment:
   `export HERMES_FIZZY_TOKEN=...`

3. Bootstrap the pairing (verifies auth, creates the workflow columns on
   the Fizzy board, records the pairing):

   ```
   hermes kanban sync init --remote-board abc123
   ```

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

## Status ↔ location mapping

The sync engine auto-creates missing columns (names configurable via
`kanban.sync.column_map`):

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

Remote → local moves use the structured verbs where possible: closing a
card completes the task (`complete_task`, so run history stays correct),
dragging to **Blocked** blocks it with `kind=needs_input`, dragging a
blocked card to **Ready** unblocks it. Transitions Hermes refuses — e.g.
promoting a dependency-gated child to Ready before its parents finish —
are pushed back: the card snaps to the column matching local truth on
the same sync pass.

Dragging a card into a column that is not in `column_map` leaves the
local status untouched (the bridge has no opinion about custom columns).

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
- Synced task bodies carry a trailing `[fizzy] <card url>` line linking
  to the counterpart card.

## Comments

Comments sync both ways with provenance prefixes:

- A local comment by `techlead` appears on the card as
  `[hermes:techlead] …` (Fizzy attributes every API comment to the
  token's user, so the prefix is the only reliable authorship signal).
- A card comment by `Dana` appears on the task thread with author
  `fizzy:Dana`.

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
  otherwise fight over cursors.
- **Failure behaviour**: per-pairing errors are logged and retried with
  jittered backoff (auth failures warn at most once per 5 minutes;
  `Retry-After` on 429s is honored). One bad card never aborts a pass —
  it's recorded in `sync status` / stats errors.
- **Trust boundary**: remote card content is untrusted input. Imported
  comments are attributed to `fizzy:<name>` authors, and the sync never
  grants remote content any authority beyond ordinary task text.
