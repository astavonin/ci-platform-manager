# CI Platform Manager

Multi-platform CI automation tool for GitLab/GitHub workflow management.

**Repo:** `~/projects/projctl`

## Quick Start

```bash
# Install
pipx install git+https://github.com/astavonin/projctl.git

# Development (editable)
pipx install -e ~/projects/projctl

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
├── __main__.py           # Entry point
├── cli.py                # CLI interface with command dispatch
├── config.py             # Multi-platform configuration
├── exceptions.py         # PlatformError and custom exceptions
├── handlers/             # Modular operation handlers
│   ├── sync.py          # Planning folder sync
│   ├── loader.py        # Load issues/epics/milestones/MRs
│   ├── creator.py       # Create issues/epics
│   ├── updater.py       # Update issues/MRs/epics/milestones
│   ├── search.py        # Search operations
│   ├── comment.py       # Post MR comments
│   ├── mr_handler.py    # Create merge requests
│   └── timelog.py       # Report and log own time
├── utils/               # Shared utilities
│   ├── config_migration.py
│   ├── git_helpers.py
│   ├── logging_config.py
│   └── validation.py
└── formatters/          # Output formatters
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
    default: ["type::feature", "development-status::backlog"]  # REQUIRED
    default_epic: ["type::epic"]  # OPTIONAL (only for creating epics)
    allowed: []  # OPTIONAL — key absent = no validation; empty list = all labels rejected

# GitHub-specific settings (future)
github:
  default_org: "organization"

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

The YAML file must contain two top-level sections: `epic` and `issues`.

```yaml
# ============================================================
# EPIC SECTION (REQUIRED)
# ============================================================
epic:
  # Option 1: Link to existing epic (recommended for adding issues to existing epics)
  id: 12  # IID of existing epic (use: projctl load &12 to verify)

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

  **Usage Patterns:**

  - **Mixed References** - Combine all formats
    - Format: `["design-task", 13, "#42"]`
    - YAML-local IDs and GitLab IIDs together

  **Examples:**

  ```yaml
  # Example 1: YAML-local dependencies only (existing behavior)
  issues:
    - id: "research"
      title: "Research Architecture"
      description: |
        # Description
        Research existing patterns

        # Acceptance Criteria
        - Research complete

    - id: "design"
      title: "Create Design"
      dependencies: ["research"]  # Blocked by research task
      description: |
        # Description
        Design based on research

        # Acceptance Criteria
        - Design approved

  # Example 2: External GitLab issue dependencies
  issues:
    - id: "new-feature"
      title: "Implement New Feature"
      dependencies:
        - 13    # Depends on existing issue #13
        - "#42" # Depends on existing issue #42
      description: |
        # Description
        Feature implementation

        # Acceptance Criteria
        - Feature complete

  # Example 3: Mixed dependencies (YAML + external)
  issues:
    - id: "integration"
      title: "Integration Testing"
      dependencies:
        - "new-feature"  # YAML-local: depends on issue in this file
        - 13             # External: depends on existing GitLab issue #13
        - "#25"          # External: depends on existing GitLab issue #25
      description: |
        # Description
        Integration tests

        # Acceptance Criteria
        - Tests pass

  # Example 4: Numeric string IDs (YAML-local, not external)
  issues:
    - id: "123"  # Valid YAML ID (string)
      title: "Task 123"
      description: |
        # Description
        Task content

        # Acceptance Criteria
        - Complete

    - id: "follow-up"
      title: "Follow-up Task"
      dependencies: ["123"]  # References YAML ID "123", NOT issue #123
      description: |
        # Description
        Depends on task above

        # Acceptance Criteria
        - Complete
  ```

  **Important Notes:**
  - YAML-local IDs require the `id` field on referenced issues
  - Numeric strings like `"123"` are treated as YAML-local IDs, not external IIDs
  - Use `#` prefix (`"#123"`) or integer (`123`) for external GitLab issue references
  - External GitLab IIDs reference issues in the same project
  - External dependencies are validated before issue creation
  - Invalid external references will fail with clear error messages
  - Use `projctl load #13` to verify external issues exist

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
projctl load 113
projctl load #113
projctl load https://gitlab.com/group/project/-/issues/113

# Epic
projctl load &21
projctl load 21 --type epic

# Milestone
projctl load %123
projctl load 123 --type milestone

# Merge Request
projctl load !134
projctl load 134 --type mr
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

# Update issue description
projctl update issue 231 --description "New description"

# Add and remove labels (repeatable flag)
projctl update issue 231 --add-label "type::fix" --remove-label "type::feature"

# Assign to a user (username — resolved to numeric ID automatically)
projctl update issue 231 --assignee alice

# Set milestone (title or iid — resolved to numeric ID automatically)
projctl update issue 231 --milestone "v2.0"

# Manage 'blocked by' links (add or remove a blocker issue)
projctl update issue 376 --add-blocker 385
projctl update issue 376 --remove-blocker 252

# Change state (issue/MR/epic)
projctl update issue 231 --state close
projctl update issue 231 --state reopen

# Update MR: title, assignee, reviewer, target branch
projctl update mr 144 --title "New title" --reviewer bob --target-branch main

# Update epic title and state
projctl update epic 37 --title "New epic title" --state close

# Update milestone due date and activate it
projctl update milestone 10 --due-date 2026-04-01 --state activate

# Preview without executing (safe — no API calls at all)
projctl update issue 231 --dry-run --title "Preview" --add-label "type::fix"
```

**Reference formats (same as `load`):**
```bash
projctl update issue 231 ...           # numeric IID
projctl update issue "#231" ...        # prefixed IID
projctl update mr "!144" ...           # MR prefixed format
projctl update mr https://gitlab.com/group/repo/-/merge_requests/144 ...  # full URL
```

**Flag reference:**

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--title` | all | New title |
| `--description` | all | New description |
| `--add-label LABEL` | all | Add label (repeatable) |
| `--remove-label LABEL` | all | Remove label (repeatable) |
| `--assignee USERNAME` | issue, mr | Assignee username (auto-resolved to numeric ID) |
| `--reviewer USERNAME` | mr only | Reviewer username (auto-resolved to numeric ID) |
| `--milestone TITLE_OR_IID` | issue, mr | Milestone by title or iid (auto-resolved to numeric ID) |
| `--target-branch BRANCH` | mr only | Change MR target branch |
| `--due-date YYYY-MM-DD` | issue, milestone | Set due date |
| `--add-blocker ISSUE` | issue only | Add "is blocked by" link to ISSUE (e.g. `252` or `#252`) |
| `--remove-blocker ISSUE` | issue only | Remove "is blocked by" link to ISSUE |
| `--state EVENT` | all (restricted) | State transition (see below) |
| `--dry-run` | all | Preview intent without any API calls |

**State event rules:**

| `--state` value | Valid for | Notes |
|-----------------|-----------|-------|
| `close` | issue, mr, epic | Closes the resource |
| `reopen` | issue, mr, epic | Reopens the resource (not valid for milestone) |
| `activate` | milestone only | Activates the milestone (not valid for issues/MRs/epics) |

**Behavior notes:**
- `--assignee` and `--reviewer` accept GitLab usernames; the tool resolves them to numeric user IDs before sending the API request.
- `--milestone` accepts a milestone title (e.g. `"v2.0"`) or iid (e.g. `"5"`); resolved to the numeric database ID automatically.
- `--dry-run` is fully safe: no API calls are made, not even the read needed for label merging. Label intent is shown as `<add: [...], remove: [...]>`.
- At least one update flag must be provided; running with no flags returns an error.
- Type-specific flags (`--reviewer`, `--target-branch`, `--due-date`) are rejected with an error if used on the wrong resource type.

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

**`approval` field behaviour:**
- `approved` (default): calls `glab mr approve` after posting comments. Already-approved MRs are treated as success.
- `changes_requested`: calls `glab mr unapprove` to revoke any prior approval. MRs with no prior approval are treated as success.
- `none`: posts comments only; takes no approval action.

**Exit codes:**
- 0: All comments posted and approval action succeeded (or was already in the desired state).
- 1: One or more comments failed to post, a thread failed to resolve, or the approval action failed.

**Create merge request:**
```bash
projctl create-mr --title "Add feature X" --draft
projctl create-mr --fill --reviewer alice --label "type::feature"
projctl create-mr --target-branch develop --milestone "v2.0"
projctl create-mr --dry-run
```

### Timelog

Report your own logged time for a date or an inclusive date range, or log new time against an issue or MR. GitLab only.

```bash
projctl timelog                                  # today
projctl timelog 2026-08-05                       # that day
projctl timelog 2026-08-05 --to 2026-08-12       # inclusive interval

projctl timelog add 478 2h                        # log 2h to issue #478, today
projctl timelog add "!235" 30m --date 2026-08-05 --dry-run
```

The report date window is local calendar days (built from the machine's system timezone, no config key or flag), never deduplicates entries, and hard-errors if the authenticated GitLab user cannot be resolved rather than silently reporting an empty result. Rows gain a project-name prefix once the queried window spans multiple projects, and an entry with neither an issue nor an MR attached renders as `(no issue/MR) <project>` rather than being dropped.

Pagination follows `pageInfo.hasNextPage`/`endCursor` and is capped at 200 pages (20,000 entries). Three shapes are hard errors rather than a silently truncated report: `hasNextPage=true` with no usable cursor to continue from, a page repeating a cursor already seen this call, and exceeding the 200-page cap — each names the likely cause and suggests narrowing the date window.

The printed grand total is always the sum of the report's own rows, never the server's. GitLab's server-computed `totalSpentTime` (from the first page where it is present) is used only as a cross-check, logged as a warning on disagreement — which can happen legitimately, since GitLab computes it *before* authorization filtering ([gitlab-org/gitlab#425747](https://gitlab.com/gitlab-org/gitlab/-/issues/425747)).

`timelog add` accepts `478`/`#478`/a URL for an issue and `!235`/a URL for an MR, and passes `<DURATION>` through to GitLab unparsed (`2h`, `30m`, `1h 30m`) rather than validating its grammar client-side. `--date` is the local calendar date the time was spent; `spentAt` is normally built at local noon on that date, not local midnight, so a write and a subsequent report agree on which local day the entry belongs to at any UTC offset — except for today, where noon can itself be in the future (any entry logged before local noon), so `spentAt` is clamped to `min(local noon, now)` whenever that would otherwise happen. A `--date` later than today is rejected client-side before any API call, not silently clamped or left for GitLab to reject. `--dry-run` makes zero API calls. Unlike the report form, `add` needs project scope *and* a target host to resolve an issue/MR to its GraphQL global ID and log time on the right GitLab instance: a full URL carries both its own project and host; a bare/prefixed reference takes both from the current directory's git remote, and fails loudly if there is no git remote at all, or if the remote resolves to a known non-GitLab host (`github.com`). The resolved host travels explicitly on every GraphQL call via `glab`'s `--hostname` flag, so the global-ID lookup and the mutation cannot silently disagree on which instance they target.

`timelog` (both forms) runs with no config file present anywhere. `cmd_timelog` is the only command that constructs `Config` and tolerates its `FileNotFoundError` — every *other* command that needs `Config` hard-fails on that same exception (`cmd_wiki` and `cmd_comment` are not counterexamples either way: neither constructs a `Config` at all). Report needs no `default_group` or project scope; `add` resolves project scope from the git remote instead, and both forms are GitLab-only by nature with their own host guards (`currentUser` null check for report; the git-remote host check described above for `add`).

**Handler:** `handlers/timelog.py` — `TimelogHandler` class

### Planning Folder Synchronization

**Sync commands:**
```bash
# Push local planning → Google Drive
projctl sync push
projctl sync push --dry-run

# Pull Google Drive → local planning
projctl sync pull
projctl sync pull --dry-run
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

2. Configure Google Drive path in `config.yaml`:
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

### Setup (first time only)

```bash
make install   # creates .venv with dev tools (pytest, pylint, mypy, black, flake8)
               # also registers the CLI globally via pipx
```

Two separate environments are created:
- `.venv/` — isolated dev tooling (pytest, linters). Uses pip internally, but is sandboxed from the system Python.
- pipx environment — the `projctl` CLI installed system-wide. **Never install this with bare `pip install -e .`** — use pipx only.

### After Modifying projctl

Reinstall the CLI via pipx so the system-wide `projctl` command picks up the changes:

```bash
pipx install -e ~/projects/projctl --force
```

No need to re-run `make install` — the `.venv` dev tools do not need reinstalling after code changes.

### Running Tests

```bash
make test                                              # run full suite with coverage
.venv/bin/pytest tests/test_config.py -v              # single module
.venv/bin/pytest tests/test_config.py::TestConfig::test_planning_sync_config -v
```

### Linting

```bash
make lint      # pylint + flake8 + mypy (all must pass)
make pylint    # pylint only
make format    # apply black formatting
```

**Project Standards:**
- pylint score: >= 9.5/10
- flake8: Zero violations
- mypy: Zero type errors
- black: All files formatted

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

   # In main():
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
# Standard
pipx install git+https://github.com/astavonin/projctl.git

# Development (editable)
pipx install -e ~/projects/projctl
make install  # sets up .venv with dev deps
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
Solution: Create config.yaml in project root or use --config flag
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
Solution: pipx install -e ~/projects/projctl --force
```

**Issue: Import errors**
```
Solution: Ensure in correct directory, reinstall: pipx install -e ~/projects/projctl --force
```

## Additional Resources

- **Config example**: `config.yaml`
- **Development**: `make install && make test`
