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
│   ├── comment.py             # Post MR/PR review comments
│   ├── creator.py             # Create issues/epics/milestones (GitLab)
│   ├── github_creator.py      # Create issues (GitHub)
│   ├── github_loader.py       # Load issues/PRs/milestones (GitHub)
│   ├── github_mr_handler.py   # Create pull requests (GitHub)
│   ├── github_search.py       # Search issues/milestones (GitHub)
│   ├── github_updater.py      # Update issues/PRs (GitHub)
│   ├── labels.py              # Display configured labels
│   ├── loader.py              # Load issues/epics/milestones/MRs (GitLab)
│   ├── mr_handler.py          # Create merge requests (GitLab)
│   ├── pipeline_handler.py    # Debug failed pipeline jobs (GitLab)
│   ├── search.py              # Search operations (GitLab)
│   ├── sync.py                # Planning folder sync (Google Drive)
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

# Activate milestone and set due date
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
| `--due-date YYYY-MM-DD` | milestone only | Set due date |
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
```

**`approval` field behaviour:**
- `approved` (default): calls `glab mr approve` after posting comments. Already-approved MRs are treated as success.
- `changes_requested`: calls `glab mr unapprove` to revoke any prior approval. MRs with no prior approval are treated as success.
- `none`: posts comments only; takes no approval action.

**Exit codes:**
- 0: All comments posted and approval action succeeded (or was already in the desired state).
- 1: One or more comments failed to post, or the approval action failed.

**Create merge request / pull request:**
```bash
projctl create-mr --title "Add feature X" --draft
projctl create-mr --fill --reviewer alice --label "type::feature"
projctl create-mr --target-branch develop --milestone "v2.0"
projctl create-mr --dry-run
```

Platform dispatch: uses `gh pr create` for GitHub, `glab mr create` for GitLab, based on `config.platform`.

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
