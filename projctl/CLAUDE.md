# CI Platform Manager

Multi-platform CI automation tool for GitLab/GitHub workflow management.

## Quick Start

```bash
# First-time setup (creates .venv and installs CLI via pipx)
make install

# Basic usage
projctl --help

# Planning sync (most common)
projctl sync push
projctl sync pull --dry-run
```

## Architecture

**Package Structure:**
```
projctl/
├── __init__.py
├── __main__.py                # Entry point
├── cli.py                     # CLI interface with command dispatch
├── config.py                  # Multi-platform configuration
├── exceptions.py              # PlatformError and custom exceptions
├── handlers/                  # Modular operation handlers
│   ├── artifacts_handler.py   # Download CI job artifacts (GitLab)
│   ├── ci_lint.py             # Validate CI configuration (GitLab)
│   ├── ci_run.py              # Create and await a pipeline (GitLab)
│   ├── comment.py             # Post MR/PR review comments
│   ├── creator.py             # Create issues/epics/milestones (GitLab)
│   ├── github_creator.py      # Create issues (GitHub)
│   ├── github_loader.py       # Load issues/PRs/milestones (GitHub)
│   ├── github_mr_handler.py   # Create pull requests (GitHub)
│   ├── github_search.py       # Search issues/milestones (GitHub)
│   ├── github_updater.py      # Update issues/PRs (GitHub)
│   ├── labels.py              # Display configured labels
│   ├── loader.py              # Load issues/epics/milestones/MRs (GitLab)
│   ├── merge.py               # Merge MRs, single or stacked chain (GitLab)
│   ├── mr_handler.py          # Create merge requests (GitLab)
│   ├── pipeline_handler.py    # Debug failed pipeline jobs (GitLab)
│   ├── search.py              # Search operations (GitLab)
│   ├── sync.py                # Planning folder sync (Google Drive)
│   ├── timelog.py             # Report and log own time (GitLab)
│   ├── updater.py             # Update issues/MRs/epics/milestones (GitLab)
│   └── wiki.py                # Manage GitLab project wiki pages
├── utils/                     # Shared utilities
│   ├── cli_runner.py
│   ├── config_migration.py
│   ├── gh_runner.py           # GitHub CLI runner (gh)
│   ├── git_helpers.py
│   ├── glab_runner.py         # GitLab CLI runner (glab)
│   ├── logging_config.py
│   ├── mr_builder.py
│   └── validation.py
└── formatters/                # Output formatters
    ├── ticket_formatter.py
    └── utils.py
```

**Design Principles:**
- Modular handler-based architecture
- Multi-platform support (GitLab, GitHub)
- Dry-run mode for all operations
- Type hints throughout
- Comprehensive error handling

## Configuration

### Config File Resolution

Search order (first found wins):
1. `--config` flag (explicit path)
2. `./glab_config.yaml` (project-local, legacy)
3. `./projctl.yaml` (project-local, preferred)
4. `~/.config/projctl/config.yaml` (user-wide)
5. `~/.config/glab_config.yaml` (legacy)

### Config Structure

```yaml
# Platform selection
platform: gitlab  # or github

# GitLab-specific settings
gitlab:
  default_group: "group/project"  # REQUIRED for epic operations
  labels:
    # REQUIRED — applied to every new issue.
    # Supports two item types:
    #   - Plain string: always applied as a default label.
    #   - Inner list (OR group): exactly one member must be present on each issue.
    #     Issue creation fails with a clear error if zero or more than one member is present.
    default:
      - ["type::feature", "type::bug"]   # OR group — pick exactly one
      - "development-status::backlog"     # flat label — always applied
    default_epic: ["type::epic"]  # OPTIONAL (only for creating epics)
    allowed: []                   # OPTIONAL — key absent = no validation; empty list = all labels rejected

# GitHub-specific settings
github:
  repo: "org/repo"   # OPTIONAL — auto-detected from git remote when absent
  labels:
    default:
      - ["type::feature", "type::bug"]
      - "development-status::backlog"

# Common settings
common:
  issue_template:
    required_sections:
      - "Description"
      - "Acceptance Criteria"
  mr_template:
    required_sections:
      - "Summary"
      - "Implementation Details"
      - "How It Was Tested"
    required_fields:
      - "reviewers"    # enforce: at least one reviewer specified (CLI or default)
      - "labels"       # enforce: at least one label
    reviewers:         # always added to every MR/PR; merged with --reviewer (deduplicated)
      - alice
      - bob

# Planning sync settings
planning_sync:
  gdrive_base: ~/GoogleDrive  # Machine-specific path
```

**Required vs Optional Fields:**

**REQUIRED:**
- `gitlab.default_group` - Needed for epic and group-level operations
- `labels.default` - Default labels applied to new issues

**OPTIONAL:**
- `labels.default_epic` - Only needed when *creating* epics (not for loading)
- `labels.allowed` (or `allowed_labels` for legacy configs) - Enforced on every path that accepts a label: `create` (issues), `update --add-label` (issue/MR/epic), and `create-mr` (MR/PR). Key absent → no validation; empty list → all labels rejected.
- All other sections depend on features used

**OR Group Validation:**

When `labels.default` contains an inner list, it is treated as an OR group. On every `projctl create` run the tool checks that exactly one member of each OR group appears in the final label set (config defaults merged with issue-level labels). If zero or more than one member is found, issue creation is aborted with a `ValueError` before any API call is made.

```yaml
# Valid — type::bug satisfies the OR group
labels: ["type::bug"]

# Error — no type:: label present
labels: ["development-status::in-progress"]

# Error — two members of the same group
labels: ["type::feature", "type::bug"]
```

Config errors (empty OR group, non-string members, unexpected types) are caught at config-access time and raise `ConfigurationError` with a descriptive message naming the offending entry.

**Legacy Config Support:**

The tool automatically handles legacy `glab_config.yaml` format:
- Accepts `allowed_labels` (converts to `allowed`)
- Makes `default_epic` optional (defaults to empty list)
- Transforms old config structure to new format in-memory

## Commands

### Issue/Epic Management

**Create issues from YAML:**
```bash
projctl create epic_definition.yaml
projctl create --dry-run epic_definition.yaml
projctl create --config custom_config.yaml epic_definition.yaml
```

**YAML Structure for Creating Issues:**

The YAML file supports an optional `milestone` section, a required `epic` section, and a required `issues` section.

```yaml
# ============================================================
# MILESTONE SECTION (OPTIONAL — creates or links a milestone)
# ============================================================
milestone:
  title: "v2.0"
  description: "Second major release"  # optional
  due_date: "2026-12-31"              # optional, YYYY-MM-DD

# ============================================================
# EPIC SECTION (REQUIRED)
# ============================================================
epic:
  # Option 1: Link to existing epic
  id: 12  # IID of existing epic (use: projctl load epic 12 to verify)

  # Option 2: Create new epic
  # title: "Epic Title"  # REQUIRED if creating new epic
  # description: "Epic description"  # Optional
  # labels: ["type::epic", "component::feature"]  # Optional, merged with config defaults

# ============================================================
# ISSUES SECTION (REQUIRED - at least one issue)
# ============================================================
issues:
  # ---- Example 1: Minimal issue ----
  - title: "Simple Issue Title"  # REQUIRED
    description: |  # REQUIRED (must contain required sections from config)
      # Description
      Brief description of the issue

      # Acceptance Criteria
      - Criteria 1
      - Criteria 2

  # ---- Example 2: Full-featured issue ----
  - id: "issue-1"  # Optional YAML-local ID for dependency tracking
    title: "[Impl] Feature Implementation"  # REQUIRED
    description: |  # REQUIRED
      # Description
      Detailed description of what needs to be implemented

      # Acceptance Criteria
      - Unit tests pass
      - Integration tests pass
      - Documentation updated

      # Additional Notes
      This is optional if configured
    labels: ["priority::high", "component::backend"]  # Optional, merged with defaults
    assignee: "alice"  # Optional - GitLab username
    milestone: "v2.0"  # Optional - milestone title
    due_date: "2026-03-15"  # Optional - YYYY-MM-DD format

  # ---- Example 3: Issue with dependencies ----
  - id: "issue-2"  # REQUIRED if using dependencies
    title: "Dependent Issue"
    description: |
      # Description
      This issue depends on issue-1 being completed first

      # Acceptance Criteria
      - Dependency resolved
      - Feature implemented
    dependencies: ["issue-1"]  # List of YAML IDs this issue depends on
```

**Field Reference:**

**Milestone Section (optional):**
- `title` (string, REQUIRED if section present) - Milestone title
- `description` (string, optional) - Milestone description
- `due_date` (string, optional) - Due date in YYYY-MM-DD format

**Epic Section:**
- `id` (int, REQUIRED if using existing epic) - IID of existing epic
- `title` (string, REQUIRED if creating new epic) - Epic title
- `description` (string, optional) - Epic description (markdown)
- `labels` (list, optional) - Labels to add (merged with `config.labels.default_epic`)

**Issue Section (each item):**
- `id` (string, optional) - YAML-local identifier for dependency tracking
- `title` (string, REQUIRED) - Issue title
- `description` (string, REQUIRED) - Issue description with required sections
- `labels` (list, optional) - Labels to add (merged with `config.labels.default`)
- `assignee` (string, optional) - GitLab username
- `milestone` (string, optional) - Milestone title (not ID)
- `due_date` (string, optional) - Due date in YYYY-MM-DD format
- `dependencies` (list, optional) - Issues this issue depends on (blocks this issue)

  **Three Reference Formats Supported:**

  1. **YAML-local IDs** - Reference issues in the same YAML file
     - Format: `["research-task", "design-task"]`
     - Uses the `id` field from other issues in same YAML
     - Includes numeric strings like `"123"` (treated as YAML IDs)

  2. **GitLab IIDs (integer)** - Reference existing GitLab issues
     - Format: `[13, 42]`
     - Direct integer values are GitLab issue IIDs

  3. **GitLab IIDs (string)** - Reference existing GitLab issues
     - Format: `["#13", "#42"]`
     - String format with `#` prefix

  **Mixed References** — combine all formats: `["design-task", 13, "#42"]`

  **Important Notes:**
  - YAML-local IDs require the `id` field on referenced issues
  - Numeric strings like `"123"` are treated as YAML-local IDs, not external IIDs
  - Use `#` prefix (`"#123"`) or integer (`123`) for external GitLab issue references
  - External GitLab IIDs reference issues in the same project
  - External dependencies are validated before issue creation
  - Invalid external references will fail with clear error messages
  - Use `projctl load issue 13` to verify external issues exist

**General Important Notes:**
1. Epic must have EITHER `id` (existing) OR `title` (new)
2. Issue descriptions MUST contain required sections from config
3. Labels are automatically merged with config defaults
4. Dependencies support both YAML-local IDs and external GitLab IIDs (see above)
5. Use `--dry-run` to preview before creating
6. Replace example values (alice, v2.0, etc.) with your actual project values

**Load information:**
```bash
# Issue
projctl load issue 113
projctl load issue "#113"
projctl load issue https://gitlab.com/group/project/-/issues/113

# Epic
projctl load epic 21
projctl load epic "&21"

# Milestone
projctl load milestone 123
projctl load milestone "%123"

# Merge Request
projctl load mr 134
projctl load mr "!134"
```

**Search:**
```bash
projctl search issues "streaming"
projctl search issues "bug" --state opened --limit 10
projctl search epics "video"
projctl search milestones "v1.0" --state active
```

### Update Resources

**Update issues, MRs, epics, and milestones:**
```bash
# Update issue title
projctl update issue 231 --title "New title"

# Add and remove labels (repeatable)
projctl update issue 231 --add-label "type::fix" --remove-label "type::feature"

# Assign to a user (username auto-resolved to numeric ID)
projctl update issue 231 --assignee alice

# Set milestone (title or iid auto-resolved to numeric ID)
projctl update issue 231 --milestone "v2.0"

# Assign issue to an epic
projctl update issue 231 --epic "&47"

# Set story-point weight in hours
projctl update issue 231 --weight 3

# Manage 'blocked by' links (add or remove a blocker issue)
projctl update issue 376 --add-blocker 385
projctl update issue 376 --remove-blocker 252

# Close / reopen (issue, MR, epic)
projctl update issue 231 --state close
projctl update epic 37 --state reopen

# Update MR: reviewer, target branch
projctl update mr 144 --reviewer bob --target-branch main

# Set a due date (issue or milestone)
projctl update issue 231 --due-date 2026-04-01
projctl update milestone 10 --due-date 2026-04-01 --state activate

# Preview without executing — no API calls at all
projctl update issue 231 --dry-run --title "Preview" --add-label "type::fix"
```

**Reference formats:**
```bash
projctl update issue 231 ...        # numeric IID
projctl update issue "#231" ...     # prefixed IID
projctl update mr "!144" ...        # MR prefix
projctl update mr https://gitlab.com/group/repo/-/merge_requests/144 ...
```

**Flag reference:**

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--title` | all | New title |
| `--description` | all | New description |
| `--add-label LABEL` | all | Add label (repeatable) |
| `--remove-label LABEL` | all | Remove label (repeatable) |
| `--assignee USERNAME` | issue, mr | Username; auto-resolved to numeric user ID |
| `--reviewer USERNAME` | mr only | Username; auto-resolved to numeric user ID |
| `--milestone TITLE_OR_IID` | issue, mr, epic | Title or iid; auto-resolved to numeric milestone ID |
| `--target-branch BRANCH` | mr only | Change MR target branch |
| `--due-date YYYY-MM-DD` | issue, milestone | Set due date |
| `--epic REF` | issue only | Assign issue to epic (e.g. `&47`) |
| `--weight N` | issue only | Story-point weight in hours |
| `--add-blocker ISSUE` | issue only | Add "is blocked by" link to ISSUE (e.g. `252` or `#252`) |
| `--remove-blocker ISSUE` | issue only | Remove "is blocked by" link to ISSUE |
| `--state EVENT` | all (restricted) | State transition (see below) |
| `--dry-run` | all | Show intent without any API calls |

**State event rules:**

| `--state` value | Valid for | Rejected for |
|-----------------|-----------|--------------|
| `close` | issue, mr, epic | — |
| `reopen` | issue, mr, epic | milestone |
| `activate` | milestone | issue, mr, epic |

**Key behaviors:**
- `--assignee` / `--reviewer` accept GitLab usernames and are resolved to numeric IDs via `glab api users?username=<name>`.
- `--milestone` accepts a milestone title or iid and is resolved to the numeric database ID via the milestones API.
- `--dry-run` performs zero API calls (label reads are also skipped; intent is shown as `<add: [...], remove: [...]>`). `--remove-blocker` is a partial exception: it must read the issue's current links to look up the internal link ID, so dry-run performs one GET.
- `--add-blocker` / `--remove-blocker` accept a plain number, `#N`, or a full issue URL and both target an issue in the same project. Adding a link uses `link_type=is_blocked_by`. Removing a link raises an error if no link matching the target exists.
- At least one update flag is required; otherwise an error is returned.
- Type-specific flags are validated upfront and rejected with a clear message if used on the wrong resource type.

**Handler:** `handlers/updater.py` — `TicketUpdater` class

### Merge Request Operations

**Post review comments:**
```bash
projctl comment planning/reviews/MR134-review.yaml
projctl comment review.yaml --mr 134
projctl comment review.yaml --dry-run
```

**Review YAML format:**
```yaml
mr_number: 134           # optional if passed via --mr
approval: approved       # approved | changes_requested | none  (default: approved)

findings:
  - title: "Missing null check"
    severity: High
    description: "ptr may be null on the fast path"
    location: "src/foo.cc:42"
    fix: "if (!ptr) return;"

  - title: "Unused import"
    severity: Low
    description: "import is never referenced"
    locations:
      - "src/bar.py:1"
      - "src/baz.py:3"

replies:
  - discussion_id: "abc123def456"
    body: "Fixed in the latest commit."

resolve:
  - discussion_id: "abc123def456"
```

**Where `discussion_id` comes from:** `projctl load mr <N> --comments` prints it under each comment as `` `thread: <id>` ``. It is the enclosing *thread's* id, not the note's — the `/discussions/:id` endpoints reject a note id. Every note in one thread shares the same value.

`replies:` works on any thread, including an unresolvable top-level comment (GitLab promotes an individual note to a thread on reply). `resolve:` only works on resolvable threads — the ones marked 🔴 or ✅ in the comment listing. An unmarked comment is an individual note and cannot be resolved.

To close threads without posting a review, see **Resolve Discussion Threads** below — `projctl resolve` is also how thread ids are enumerated in the first place.

**`approval` field behaviour:**
- `approved` (default): calls `glab mr approve` after posting comments. Already-approved MRs are treated as success.
- `changes_requested`: calls `glab mr unapprove` to revoke any prior approval. MRs with no prior approval are treated as success.
- `none`: posts comments only; takes no approval action.

**Exit codes:**
- 0: All comments posted and approval action succeeded (or was already in the desired state).
- 1: One or more comments failed to post, a thread failed to resolve, or the approval action failed.

**Create merge request / pull request:**
```bash
projctl create-mr --title "Add feature X" --draft
projctl create-mr --fill --reviewer alice --label "type::feature"
projctl create-mr --target-branch develop --milestone "v2.0"
projctl create-mr --dry-run
```

Platform dispatch: uses `gh pr create` for GitHub, `glab mr create` for GitLab, based on `config.platform`.

### Merge

Merge one merge request, or a stacked chain in order. GitLab only.

```bash
projctl merge 264
projctl merge 264 --dry-run
projctl merge 264 263 265                  # stacked chain, base first
projctl merge 264 --allow-failed-pipeline
projctl merge 264 --keep-branch --squash
projctl merge 264 263 --rebase             # fast-forward-only project
```

**Gates.** Every MR is checked before the merge call: it must be opened, not a draft, mergeable, free of unresolved threads, and its head pipeline must have succeeded. A missing pipeline passes — docs-only branches and projects without CI legitimately have none. Only the last two gates are waivable, with `--allow-unresolved` and `--allow-failed-pipeline`.

**`detailed_merge_status` is the authority, not `merge_status`.** The legacy field reports `can_be_merged` for an MR GitLab then refuses with HTTP 405 — `ci_must_pass` after a retarget is the case that bites, because the old pipeline ran against the old target. Values meaning "ask again shortly" (`checking`, `unchecked`, `preparing`, `ci_still_running`) are polled rather than treated as a refusal, since a retarget passes through several of them before settling.

**Chains stop at the first blockage.** Every later MR in a stack targets a branch the blocked one was supposed to move, so continuing would either fail the same way or merge into the wrong base. The summary reports how many of the chain merged.

**Retargeting is waited for, not assumed.** GitLab retargets a child MR onto the grandparent when its target MR merges, and that write is asynchronous — gating the child immediately would evaluate it against a branch that no longer exists. Only a child that actually targets the just-merged branch is waited for; independent MRs merged in the same run never move and are not reported as retargeted.

**`--rebase` is required for a stacked chain on a fast-forward-only project.** Under `merge_method: ff` the branch must be a direct descendant of its target, and a squashing project rewrites each merged commit — so a stacked MR stops being a descendant the moment its parent lands, while GitLab still reports `mergeable` and then refuses the PUT with 422. The flag rebases each remaining MR after its parent merges and waits for the new pipeline, since the rebase gives the branch a new SHA.

**A merge rejected with 405/422 while the MR reports itself mergeable is retried.** Immediately after a retarget GitLab can report `mergeable` and still refuse; the same MR merges cleanly seconds later untouched. Each retry re-runs the gates first, so a refusal that is real surfaces by name instead of looping.

**`--dry-run` does not stop early.** Nothing is being merged, so it reports every gate for every MR at once. It also lists jobs that failed under `allow_failure` as `masked` — such a pipeline reports `success` and passes the gate, so a green rollup is shown for what it is rather than silently trusted.

**Polls tolerate a transient network error.** A pipeline wait runs for tens of minutes; a single unreachable poll is logged and retried within the loop's own budget rather than aborting a merge that is otherwise on track.

**Exit codes:** `0` when every named MR merged (or, under `--dry-run`, when every one of them could), `1` on any blockage, failure, or malformed reference.

**Handler:** `handlers/merge.py` — `MergeHandler` class

### Notes (Comments)

Post a note (comment) to a GitLab issue, MR, or epic. GitLab only.

```bash
projctl note issue 340 --body "Closing as false-positive."
projctl note mr !194 --body "LGTM"
projctl note epic &64 --body "Superseded by new approach."
projctl note issue #340 --body "See also #341" --dry-run
```

Reference formats accepted per resource type:

- **issue**: `N`, `#N`, or full URL (`.../-/issues/N` or `.../-/work_items/N`)
- **mr**: `N`, `!N`, or full URL (`.../-/merge_requests/N`)
- **epic**: `N`, `&N`, or full URL (`.../groups/G/-/epics/N`)

**Epic transport:** issue and MR notes use the REST `POST /projects/:id/{issues,merge_requests}/:iid/notes` endpoint. Epic notes go via the GraphQL `createNote` mutation against the epic's backing `WorkItem` GID — GitLab 15.9+ has migrated group epics to work items, and the REST group-epic notes endpoint returns 404. The handler resolves the `work_item_id` via a REST GET on the epic first, then posts via GraphQL.

**Handler:** `handlers/note.py` — `NoteHandler` class

### Resolve Discussion Threads

Resolve (close) review discussion threads on a merge request. GitLab only.

```bash
projctl resolve mr 134 --list
projctl resolve mr 134 --match "race condition in cache invalidation"
projctl resolve mr !134 --match "SQL injection" --match "unused parameter"
projctl resolve mr 134 --discussion a1b2c3d4e5f6 --dry-run
projctl resolve mr 134 --match "missing unit test" --unresolve
```

Complements the `resolve:` key of `projctl comment`, which closes threads as part of posting a
review from a YAML file. This command resolves arbitrary threads without authoring one, and is
the only way to enumerate thread ids in the first place.

**Selectors.** `--discussion` takes a full id or a unique prefix; `--match` takes a substring
matched against a thread's *first* note. Both are repeatable and may be mixed. A selector that
hits zero threads, or more than one, is a hard error naming the candidates — never a silent
no-op and never a batch resolve. There is deliberately no `--all`: resolving the wrong thread
silently marks a review finding as handled, so every thread must be named.

**Exit codes.** `0` when every selected thread reached the target state or was already there.
`1` when an explicitly named thread was not resolvable (a system note or standalone comment), or
when its API call failed. An already-resolved thread is a genuine no-op and stays `0`.

**Partial failure.** A failing thread does not abort the run — the remaining selectors are still
attempted and the summary reports `resolved / skipped / failed`, so the record of what already
changed on the server survives the error.

**`--dry-run` still issues the read.** The discussion list must be fetched to resolve selectors
to ids, so this command makes one GET even under `--dry-run`; only the PUTs are suppressed. This
is the same exception `update --remove-blocker` documents, and differs from the tool's usual
"zero API calls" reading.

**`--list` shows system notes** (`added 1 commit`, `requested review from ...`) as `[n/a ]` rows.
They are real discussions but carry no resolvable note, so they are listed for completeness and
rejected as selector targets.

**Request shape:** `resolved` travels in the query string, never a JSON body — GitLab accepts the
body form for DiffNote threads but answers 403 for DiscussionNote ones. Both this command and
`comment.py` build the endpoint through `utils/glab_runner.py` → `discussion_resolve_endpoint()`
so the two cannot drift apart. See `planning/reviews-orphan/main-0e68987/observed-failures.md`
(2026-08-13 and 2026-08-25).

**Handler:** `handlers/resolve.py` — `ResolveHandler` class

### Timelog

Report your own logged time for a date or an inclusive date range, or log new time against an issue or MR. GitLab only.

```bash
# Report (read-only)
projctl timelog                                  # today
projctl timelog 2026-08-05                       # that day
projctl timelog 2026-08-05 --to 2026-08-12       # inclusive interval

# Log time (write)
projctl timelog add 478 2h                        # today, issue #478
projctl timelog add "#478" "1h 30m" --date 2026-08-05
projctl timelog add "!235" 30m --dry-run          # MR !235, preview only
```

Report output is a per-day total, a per-issue (or per-MR) breakdown within each day, and a grand total across the window — always preceded by the exact window queried and the GitLab identity queried as, even when the result is empty. A zero result is reported as "no timelogs returned for this window", never as a confident "you logged nothing", because an empty result caused by `glab` resolving the wrong host would look identical otherwise.

**Behavior notes — report:**
- The date window is **local calendar days**, built from the machine's system timezone — not UTC days. There is no config key or flag for the offset; output is machine-dependent by design.
- Before querying timelogs, the handler resolves the authenticated user via GraphQL `currentUser`. A `null` result is a **hard error** naming the likely cause (`glab` resolved to a host with no GitLab remote in the current directory), not an empty report — this is the command's main safety property, since a misdirected query would otherwise look exactly like "nothing logged".
- Entries are **never deduplicated**: two timelogs with identical timestamp and duration on the same issue in the same day are both counted, since GitLab's own data contains exactly that shape.
- An entry attached to a merge request rather than an issue (`issue: null`, `mergeRequest` populated) renders with an `!N` marker instead of being dropped.
- An entry with neither `issue` nor `mergeRequest` set (the schema allows both to be absent) renders as `(no issue/MR) <project>` instead of being dropped.
- Rows gain a project-name prefix (e.g. `alpha #11 Fix bug`) once the **queried window** — not the individual day — spans more than one project; a single-project window renders with no prefix, unchanged from before this behavior existed. Two projects whose short names collide (e.g. `team-a/docs` and `team-b/docs` both ending in `docs`) fall back to their full path as the prefix instead of rendering identical, indistinguishable rows.
- Results are paginated via `pageInfo.hasNextPage`/`endCursor`, never bounded on the connection's `count` field, which GitLab's own schema documents as saturating at "limit + 1" once the filtered set exceeds the page size. Pagination is capped at 200 pages (20,000 entries) — a legitimately reachable window, not just a safety margin. A page reporting `hasNextPage=true` with no usable cursor to continue from, a page repeating a cursor already seen this call, or exceeding the 200-page cap are all hard errors rather than a silently truncated report at exit code 0; each names the likely cause and suggests narrowing the date window.
- The printed grand total is always the sum of the report's own rows, never the server's. GitLab's server-computed `totalSpentTime` — read once, from the first page where the key is present — is used only as a cross-check, logged as a warning when it disagrees and skipped when absent or unparsable. Disagreement can be legitimate: GitLab computes `totalSpentTime` *before* authorization filtering ([gitlab-org/gitlab#425747](https://gitlab.com/gitlab-org/gitlab/-/issues/425747)), so a user who can no longer see every timelog they logged (e.g. after losing access to an issue) will see a server total that includes entries their own rows can't.
- No `--dry-run` flag on the report form — it is read-only, matching the convention used by `load`, `search`, and `sync status`.

**Behavior notes — add:**
- `<TARGET>` accepts the same reference forms as `note`: `478`/`#478` or a full URL for an issue, `!235` or a full URL for an MR.
- `<DURATION>` is GitLab's own duration syntax (`2h`, `30m`, `1h 30m`) passed through **unparsed** — this command never implements the grammar itself. A malformed duration is rejected by GitLab, and its message is surfaced verbatim via a `PlatformError`.
- `--date` is the **local calendar date** the time was spent, defaulting to today. `spentAt` is normally built at **local noon** on that date (not local midnight) so it round-trips correctly through the report form's local-day bucketing at any UTC offset — a naive UTC-midnight timestamp would misfile the entry into the adjacent local day at some offsets. For `--date` defaulting to *today*, noon can itself be in the future relative to the actual call time (any entry logged before local noon) — GitLab rejects a future `spentAt` outright, so `spentAt` is clamped to `min(local noon, now)` whenever that would otherwise happen; the clamp is a no-op for any date strictly before today. A `--date` later than today is rejected client-side with a `ValueError`, before any project/target resolution or API call — not silently clamped to today, and not left for GitLab to reject.
- Resolving `<TARGET>` to the GraphQL global ID `timelogCreate` needs **project scope and a target host**, neither of which the report form needs. A full issue/MR URL carries its own project path and host; a bare/prefixed reference falls back to the current directory's git remote for both (matching `note`/`wiki`), and fails with an actionable message if there is no git remote at all, or if the remote resolves to a known non-GitLab host (`github.com`) — it does **not** silently fall back to a default host. The resolved host travels explicitly on every `glab api graphql` call via `--hostname`, so the global-ID lookup and the mutation always agree on which GitLab instance they target.
- The mutation checks `TimelogCreatePayload.errors` explicitly, beyond the top-level GraphQL `errors` array, and also requires a non-null `timelog.id` in the response when `errors` is empty — GitLab can return HTTP 200 with populated `errors` and a null payload, *or* with no `errors` and no created `timelog` at all, and either shape would otherwise print success for a write that never happened. The second case is reported as an indeterminate outcome (verify with `projctl timelog` before retrying) rather than a plain failure, since `timelogCreate` has no idempotency key and the read path never dedups.
- `--dry-run` issues **zero** `glab` API calls — including the read-only global-ID lookup — while still running the local-only host/project resolution (and its `github.com` rejection), so the preview reflects the same target, project, host, and `spentAt` a real run would use.
- `timelog` (both forms) runs with **no config file present anywhere**. `cmd_timelog` is the only command that constructs `Config` and tolerates a missing one — every *other* command that needs `Config` hard-fails on that same `FileNotFoundError`; `cmd_wiki` and `cmd_comment` are not counterexamples either way, since neither constructs a `Config` at all. Report needs no `default_group` or project scope at all; add resolves project scope from the git remote instead of from config. An absent config only skips the fast platform-gate rejection; an explicitly-named `--config` path that does not exist is still a hard error.

**Handler:** `handlers/timelog.py` — `TimelogHandler` class

### Pipeline Debugging

Debug failed CI/CD pipeline jobs. GitLab only.

**By branch** — fetches all failed jobs from the latest pipeline on the branch:

```bash
projctl pipeline-debug
projctl pipeline-debug --branch feature/my-branch
```

**By job ID** — fetches logs for a single job directly, bypassing branch and pipeline discovery:

```bash
projctl pipeline-debug --job-id 5946580
```

**Handler:** `handlers/pipeline_handler.py` — `PipelineHandler` class

### CI Job Artifacts

Download files a CI job archived as artifacts. GitLab only. Complements `pipeline-debug`, which fetches job *logs* (a text trace); this fetches the archived *files*.

**Single file** — pulls one path out of the job's archive:

```bash
projctl artifacts --job-id 12345 --path build/server.stdout
projctl artifacts --job-id 12345 --path build/server.stdout --dest ./out
```

**Whole archive** — downloads and extracts everything, for when the archive's layout is unknown:

```bash
projctl artifacts --job-id 12345
projctl artifacts --job-id 12345 --dest ./out
```

**Behavior notes:**
- `--dest` defaults to the current directory and is created if missing.
- A single file is written to `<dest>/<path>`, mirroring the archive's directory layout rather than flattening to a basename — two artifacts can share a basename across directories, and flattening would silently overwrite one.
- The full-archive path streams to `<dest>/artifacts.zip` without buffering in memory, then extracts via `ZipFile.extractall()` (which sanitizes member names, making extraction zip-slip safe).
- Artifact payloads are binary, so this command uses the binary-safe transport in `utils/glab_runner.py` (`run_glab_command_binary`, `stream_glab_command_to_file`) rather than the shared `text=True` path, which corrupts or raises on non-UTF-8 bytes.

**Handler:** `handlers/artifacts_handler.py` — `ArtifactsHandler` class

### CI Configuration Lint

Validate a GitLab CI configuration against the server-side linter before pushing it. GitLab only.

```bash
projctl ci lint                             # .gitlab-ci.yml in the current directory
projctl ci lint path/to/.gitlab-ci.yml      # explicit path
projctl ci lint --dry-run                   # also simulate pipeline creation
projctl ci lint --dry-run --ref master      # simulate against a branch or tag
```

**Behavior notes:**
- The GitLab server linter is the only authority on CI schema, which is the point of the command: a file can parse as valid YAML and still be rejected. A `script` entry of `- echo "Version: $TAG"` parses as a list holding a *mapping* (`{'echo "Version': '$TAG"'}`) rather than a list of strings, and the schema refuses it — a local YAML parse sees nothing wrong. (The single-line form `script: - echo "..."` is a plain YAML syntax error and is *not* the interesting case; the block form above is.)
- **Exit codes are a three-way contract**, not a boolean: `0` valid, `1` GitLab rejected the configuration, `2` the check could not be performed at all. `2` covers an unusable flag combination, a missing file, an uninstalled `glab`, and every `glab` failure — expired token, no GitLab remote, API error. Keeping `1` and `2` apart is the point: `glab` itself exits 1 for *both* "invalid config" and "I could not check", so a caller that branches on 1 would report a broken CI file every time a token expired.
- The verdict is read from a marker on stdout (`is valid` / `is invalid`), not from the exit code alone. Output carrying neither marker raises `PlatformError` regardless of exit status — that is the only signal separating a real verdict from a tool failure, since `glab` prints `Validating...` before it calls the API and so "stdout is non-empty" proves nothing. This also fails **closed** on a `glab` wording change: the drift surfaces as exit 2 rather than silently approving every configuration.
- The linter's own report (job name, offending key, reason) is printed verbatim on a verdict, and carried in the `PlatformError` message otherwise — stderr included, since that is where every tool failure explains itself.
- `glab` signals the verdict through its exit code and writes the report to *stdout*, so this command uses `run_glab_command_status` in `utils/glab_runner.py` rather than the shared runner, which raises on a non-zero exit and keeps only stderr — discarding the report.
- `--ref` is rejected without `--dry-run`. `glab` only applies it during a pipeline simulation and silently ignores it otherwise, which would report a plain static check as if it had been validated against that branch.
- **`--dry-run` here is `glab`'s flag and inverts projctl's own convention.** Everywhere else in this tool `--dry-run` means "preview, make no API calls"; here it asks GitLab to *additionally* simulate pipeline creation — a heavier server call, not a skipped one. The CLI flag keeps `glab`'s spelling; the handler parameter is named `simulate` so the inversion cannot be mistaken for the usual meaning in code.
- The path is passed after a `--` separator so a CI file whose name begins with a dash cannot be consumed by `glab` as a flag. Flags are emitted before the separator, since one placed after it would parse as a second positional argument.
- `glab ci lint` accepts a URL as well as a local path; this command does **not**. The path is checked with `Path.is_file()` first, so a URL is rejected locally with `CI configuration not found`. The precheck is deliberate — it turns a remote round-trip into an immediate local error — but it does narrow what `glab` alone would accept.

**Handler:** `handlers/ci_lint.py` — `CiLintHandler` class

### CI Pipeline Run

Create a pipeline for a branch, optionally waiting for it to finish. GitLab only.

```bash
projctl ci run                                          # the checked-out branch
projctl ci run --branch master
projctl ci run --variable RUN_SLOW_TESTS=true --wait
projctl ci run --branch master --dry-run
```

**Behavior notes:**
- **Exit codes are a three-way contract**, matching `ci lint`'s reasoning but with different meanings: `0` the pipeline was created (and succeeded, under `--wait`), `1` it was created but did not succeed, `2` it could not be created at all. A caller that collapses 1 and 2 would report a failing build every time a token expired.
- **`--dry-run` here is projctl's usual one**, unlike `ci lint`'s: it reports the pipeline that would be created and makes no API call. The two `--dry-run` flags under `ci` deliberately differ because `lint`'s is `glab`'s own flag.
- The default ref is the checked-out branch, read via `utils/git_helpers.py` → `get_current_branch()`. A detached HEAD is a hard error naming `--branch`, since there is no branch to run against.
- A malformed `--variable` is rejected before the API call. Silently dropping one would produce a pipeline that looks right and behaves differently.
- `--wait` polls for up to an hour (240 × 15s) and returns the status GitLab reports. `manual` and `skipped` count as terminal — the pipeline will not change without another event — and only `success` exits 0. A pipeline that finished green solely because every failed job carried `allow_failure` still reports `success`; use `merge --dry-run`, which lists those jobs as `masked`, when that distinction matters.

**Handler:** `handlers/ci_run.py` — `CiRunHandler` class

### Wiki Management

Manage GitLab project wiki pages. Must be run from within a git repository with a GitLab remote.

```bash
# List all pages (slug + title)
projctl wiki list

# Load and print a page by slug
projctl wiki load my-page-slug

# Create a new page from a Markdown file
projctl wiki create "My Page Title" --content path/to/page.md
projctl wiki create "My Page Title" --content page.md --dry-run

# Update an existing page (preserves current title)
projctl wiki update my-page-slug --content updated.md
projctl wiki update my-page-slug --content updated.md --dry-run
```

**Handler:** `handlers/wiki.py` — `WikiHandler` class

### Labels

Display configured labels from the project config, grouped by prefix (`type::`, `priority::`, etc.):

```bash
projctl labels
```

Shows `allowed` labels if configured and non-empty; otherwise falls back to `default` labels with a note. OR groups from `labels.default` are always shown at the bottom under "Required (pick one per group)" regardless of whether an `allowed` list is set. GitLab and GitHub.

**Handler:** `handlers/labels.py` — `LabelsHandler` class

### Planning Folder Synchronization

**Sync commands:**
```bash
# Check drift state before syncing (read-only — run this first)
projctl sync status

# Push local planning → Google Drive
projctl sync push
projctl sync push --dry-run

# Pull Google Drive → local planning
projctl sync pull
projctl sync pull --dry-run
```

#### `sync status` — Drift Detection

Reports the relationship between `./planning/` and the Google Drive backup without modifying either side.

**Four drift states:**

| State | Meaning | Safe next action |
|-------|---------|-----------------|
| `in-sync` | Both sides identical | Either push or pull is a no-op |
| `local-ahead` | Local has changes remote does not | Run `sync push` |
| `remote-ahead` | Remote has changes local does not | Run `sync pull` |
| `diverged` | Both sides have independent changes | Manual reconciliation required |

**Output contract:**
- Line 1 of stdout is always exactly one of: `STATUS: in-sync`, `STATUS: local-ahead`, `STATUS: remote-ahead`, `STATUS: diverged`.
- Exit code 0 for every drift state (including `diverged`). Exit 1 only on genuine errors.
- The rest of stdout is a human-readable detail block listing files that would be transferred or deleted by a subsequent push or pull.

**Drift oracle:** The classification is forward-looking — it describes what a subsequent `sync push` / `sync pull` would transfer or delete, using rsync's default size+mtime comparison (no content checksum). Consequences:
- A file with identical content but different timestamps on the two sides is reported as drift. A runtime note is emitted in the detail block when only timestamp or permission attributes differ.
- A file present only on remote cannot be distinguished from a file deleted locally after a previous push; both classify as `remote-ahead`.
- An empty directory that exists only on one side produces no drift signal and classifies as `in-sync`.

**Concurrency caveat:** The two rsync dry-runs are not atomic. If either tree is mutated between them, the report may describe a state that did not exist at any single moment. `status` is idempotent — rerunning after edits settle gives the correct result.

**Recommended usage in `/start` workflows:**

Run `sync status` before `sync pull` on every machine start. If the result is `local-ahead` or `diverged`, do NOT pull — show the user the file lists and recommend the appropriate action first.

```bash
# Recommended /start sequence
projctl sync status   # read-only check
# If in-sync or remote-ahead → proceed with pull
projctl sync pull
# If local-ahead → push first, then verify
projctl sync push
```

## Planning Sync Deep Dive

### Purpose

Synchronize proprietary planning folders across multiple machines using Google Drive as centralized backup.

**Use cases:**
- Work on planning docs from multiple machines (desktop, laptop)
- Backup planning folders automatically
- Keep planning folders in sync without git commits

**Claude memory** — `sync push/pull` also syncs the Claude project memory directory (`~/.claude/projects/<encoded-repo-path>/memory/`) to `${GDRIVE_BASE}/backup/claude-memory/<encoded-repo-path>/`. Memory is private and excluded from git; this sync is its only cross-machine backup. The sync is skipped silently when the memory directory does not exist. `sync status` appends a `Memory STATUS:` section below the planning report.

### Architecture

**Auto-Detection:**
- Repository name: Extracted from git repository directory name
- Planning folder: Always `./planning/` from repository root
- No manual configuration of repo name or paths needed

**Google Drive Structure:**
```
${GDRIVE_BASE}/backup/planning/
├── genai-automations/    # Auto-created on first push
│   ├── progress.md
│   └── ci-platform-refactor/
└── other-repos/          # Other repositories sync here automatically

${GDRIVE_BASE}/backup/claude-memory/
└── -home-alice-projects-genai-automations/   # Encoded repo root path
    └── MEMORY.md
```

**Sync Strategy:**
- Uses `rsync` with `--delete` flag (last write wins)
- Uses `--inplace` so the Google Drive client records a revision instead of creating a duplicate object (`name (2).ext`)
- Excludes: `*.swp`, `*~`, `.DS_Store`, `.workflow-safety.log`, `memory`
- Efficient incremental sync (only changed files)
- No version history (Google Drive provides 30-day file versioning)

### Setup (Per Machine)

**Initial setup on new machine:**

1. Install dependencies:
   ```bash
   # Ensure rsync is installed
   which rsync || sudo apt install rsync  # Ubuntu/Debian

   # Ensure Google Drive is mounted and synced
   ls ~/GoogleDrive  # Verify path
   ```

2. Configure Google Drive path in `projctl.yaml`:
   ```yaml
   planning_sync:
     gdrive_base: ~/GoogleDrive  # Adjust for your mount point
   ```

3. Pull existing planning folder:
   ```bash
   cd ~/projects/genai-automations
   projctl sync pull --dry-run  # Preview
   projctl sync pull            # Execute
   ```

**Repeat for each repository with planning folder**

### Regular Workflow

**Machine A (after making changes):**
```bash
cd ~/projects/genai-automations
# Work on planning docs...
projctl sync push
# Google Drive auto-syncs to cloud (usually within seconds)
```

**Machine B (before starting work):**
```bash
cd ~/projects/genai-automations
projctl sync pull   # Get latest changes
# Work on planning docs...
projctl sync push   # Push changes back
```

**Best Practices:**
- Always `pull` before starting work
- Always `push` after finishing work
- Use `--dry-run` when unsure
- Check Google Drive sync status before switching machines

### Error Handling

**Common errors and solutions:**

1. **Planning folder not found:**
   ```
   Error: Planning folder not found: /path/to/repo/planning
   ```
   Solution: Create planning folder or check you're in correct repo

2. **Google Drive not mounted:**
   ```
   Error: Google Drive not found: ~/GoogleDrive
   ```
   Solution: Verify Google Drive path in config, ensure it's mounted

3. **Not in git repository:**
   ```
   Error: Not in a git repository. Planning sync requires git.
   ```
   Solution: Run command from within git repository

4. **rsync not installed:**
   ```
   Error: rsync is not installed or not available in PATH
   ```
   Solution: `sudo apt install rsync` (Ubuntu/Debian)

### Implementation Details

**Handler: `handlers/sync.py`**

**Key Class: `PlanningSyncHandler`**

Methods:
- `__init__(config, dry_run)` - Initialize with config and dry-run mode
- `push()` - Push local planning → Google Drive
- `pull()` - Pull Google Drive → local planning
- `_detect_repo_name()` - Auto-detect repository name from git
- `_get_planning_path()` - Get planning folder path (./planning/)
- `_verify_rsync_available()` - Verify rsync is installed
- `_run_rsync(source, target, description)` - Execute rsync command

**Auto-detection logic:**
```python
# Repo name from git repository directory name
repo_root = subprocess.run(['git', 'rev-parse', '--show-toplevel'])
repo_name = Path(repo_root).name  # e.g., "genai-automations"

# Planning path
planning_path = repo_root / 'planning'

# Google Drive path
gdrive_repo_path = gdrive_base / 'backup' / 'planning' / repo_name
```

**Rsync command:**
```bash
rsync -av --inplace --delete \
  --exclude='*.swp' \
  --exclude='*~' \
  --exclude='.DS_Store' \
  --exclude='.workflow-safety.log' \
  --exclude='memory' \
  source/ target/
```

**Memory pull does not use `--delete`** — local-only memory files are preserved to prevent accidental data loss (memory has no git backup).

**`--inplace` is load-bearing.** Without it rsync writes a temp file and renames it over the target. The Google Drive client sees the old inode vanish and a new one appear, so it uploads a *new* Drive object rather than a revision of the existing one — and because Drive keys files by ID rather than name, two same-named objects can coexist in one folder and the client disambiguates the second as `name (2).ext` when materializing the folder back onto a POSIX filesystem. Writing in place preserves the inode. Trade-off: `--inplace` is not atomic, so a crash mid-write leaves a partially written file instead of the previous version intact — acceptable for Markdown/YAML planning files recoverable from git or the other side of the sync.

## Development

### Setup

```bash
make install   # creates .venv, installs dev deps, registers CLI via pipx
```

### Running Tests

```bash
make test                                              # run full suite with coverage
.venv/bin/pytest tests/test_config.py -v              # single module
.venv/bin/pytest tests/test_config.py::TestConfig::test_planning_sync_config -v
```

### Linting

```bash
make lint      # pylint + flake8 + mypy
make pylint    # pylint only
make format    # apply black formatting
```

Individual linters (all in `.venv/bin/`):
```bash
.venv/bin/pylint projctl/ --rcfile=pyproject.toml
.venv/bin/flake8 projctl/          # config: .flake8 (max-line-length=120, extend-ignore=E203)
.venv/bin/mypy projctl/ --config-file=pyproject.toml
.venv/bin/black projctl/ --check   # check only
.venv/bin/black projctl/           # apply
```

**Project Standards:**
- pylint score: >= 9.5/10
- flake8: zero violations
- mypy: zero type errors
- black: all files formatted

### Adding New Handlers

**Pattern to follow:**

1. Create handler file: `projctl/handlers/new_handler.py`

2. Implement handler class:
   ```python
   from ..config import Config
   from ..exceptions import PlatformError

   class NewHandler:
       """Handler for new operation."""

       def __init__(self, config: Config, dry_run: bool = False) -> None:
           self.config = config
           self.dry_run = dry_run

       def execute(self) -> None:
           """Execute the operation."""
           # Implementation
   ```

3. Add to CLI: `projctl/cli.py`
   ```python
   from .handlers.new_handler import NewHandler

   def cmd_new(args) -> int:
       config = Config(args.config)
       handler = NewHandler(config, dry_run=args.dry_run)
       handler.execute()
       return 0

   # In main(): register subparser and add to commands dict
   commands = {
       # ...
       'new': cmd_new,
   }
   ```

4. Write tests: `tests/handlers/test_new_handler.py`

## Dependencies

### Runtime Dependencies

- **Python** >= 3.7
- **PyYAML** >= 5.4 - YAML parsing
- **glab** CLI - GitLab operations
- **gh** CLI - GitHub operations
- **rsync** - Planning folder sync (system package)
- **Google Drive** client - Planning folder sync

### Development Dependencies

- **pytest** >= 7.0 - Testing framework
- **pytest-cov** >= 4.0 - Coverage reporting
- **pylint** >= 3.0 - Code quality
- **flake8** >= 6.0 - Style checking
- **mypy** >= 1.0 - Type checking
- **black** >= 23.0 - Code formatting
- **types-PyYAML** >= 6.0 - Type stubs

### Installation

```bash
# First-time development setup (recommended)
make install   # installs .venv deps + registers CLI via pipx

# Runtime only (no dev deps)
pipx install git+https://github.com/astavonin/projctl.git
```

## Troubleshooting

### Planning Sync Issues

**Issue: Sync fails with permission error**
```
Solution: Check Google Drive sync status, ensure folder is fully synced
```

**Issue: Wrong repository name detected**
```
Solution: Check git repository name with: git rev-parse --show-toplevel
```

**Issue: Conflict - files modified on both machines**
```
Solution: Last write wins. Pull latest, manually merge if needed, push
```

### General Issues

**Issue: Config file not found**
```
Solution: Create projctl.yaml in project root or use --config flag
```

**Issue: KeyError: 'default_epic' when loading config**
```
Solution: This is a legacy config issue. Update to latest version where default_epic is optional.
The tool now handles configs without default_epic automatically.
```

**Issue: Legacy config with allowed_labels not working**
```
Solution: Both 'allowed' and 'allowed_labels' are now supported automatically.
No manual config changes needed - the tool handles both formats.
```

**Issue: Cannot load epic**
```
Solution: Ensure gitlab.default_group is set in config.
default_epic labels are NOT required for loading epics (only for creating them).
```

**Issue: Command not found**
```
Solution: Run make install, or use python3 -m projctl
```

**Issue: Import errors**
```
Solution: Ensure in correct directory, run make install to reinstall
```

## Additional Resources

- **Config example**: `config.yaml`
- **Virtual env**: `.venv/` (created by `make install`)
