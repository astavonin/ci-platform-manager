# projctl

`projctl` is the operational CLI companion to [genai-automations](https://github.com/astavonin/genai-automations), a structured 8-phase AI-assisted development workflow where Claude Code orchestrates research, design, review, implementation, and verification through artifacts kept in a local `planning/` folder. `projctl` turns those artifacts into real work on GitLab or GitHub — creating tickets from design docs, posting review findings to merge requests, and keeping the `planning/` folder synchronized across machines.

## Why it exists

- Review artifacts (`code-review.md`, `design-review.md`, decision records) need to reach GitLab or GitHub as inline MR comments and structured tickets — `projctl` is the bridge without leaving the terminal.
- Ticket and MR operations are repetitive web-UI work that breaks flow; YAML-driven inputs remove the friction and make the operations reproducible.
- The `planning/` folder is the workflow's persistent memory across session resets and machine switches — without sync it lives on one machine only, and the workflow loses continuity.
- Every mutating operation has `--dry-run`, so workflow checkpoints are safe to explore before anything reaches the platform.

## What it does

### Ticket management

Create issues and epics from YAML files that mirror the shape of the planning artifacts, so a design doc can drive ticket creation directly. Dependencies wire between issues in the same YAML and against existing tickets, load and search work across issues, epics, milestones, and MRs by short reference or URL, and update lets you change titles, labels, assignees, milestones, and states in bulk without touching the web UI.

### MR workflow

The `comment` command is the bridge between a `code-review.md` artifact and the actual merge request: it posts inline diff comments per finding, replies to and resolves discussion threads, and issues approve or unapprove — all from a single YAML file. `create-mr` opens the MR itself with template enforcement, so required sections and default reviewers are applied consistently across every submission. `merge` closes the loop: it gates each MR on state, draft status, mergeability, unresolved threads, and its head pipeline before merging, and can land a stacked chain in order — waiting for GitLab to retarget each remaining MR, and rebasing them where the project is fast-forward only.

### Planning sync

`projctl sync` rsyncs the local `./planning/` folder to Google Drive with drift detection: `sync status` reports `in-sync`, `local-ahead`, `remote-ahead`, or `diverged` before any files move, so switching machines starts from a known state. This is what keeps the workflow's persistent context alive across session resets — pull before starting work, push when finishing, and the same planning tree is available on the next machine.

Utilities cover GitLab wiki management, CI pipeline failure log retrieval, `ci lint` — validating a `.gitlab-ci.yml` against the GitLab server-side linter before pushing it, which catches schema errors a local YAML parse cannot — `ci run` to trigger a pipeline for a branch and optionally wait on it, label inspection, configuration introspection, and `timelog`, a report of your own logged time by day and issue, plus `timelog add` to log new time against an issue or MR.

## Relationship to genai-automations

`projctl` is designed to be used alongside [genai-automations](https://github.com/astavonin/genai-automations), not as a standalone tool. The two connect at three points: `projctl sync` keeps the `planning/` folder alive across sessions and machines so the workflow's persistent memory survives; `projctl comment` posts findings from `code-review.md` and `design-review.md` artifacts back to the MR being reviewed; and `projctl create` turns design artifacts into tickets and epics with the dependency structure the workflow expects.

## Installation

```bash
pipx install git+https://github.com/astavonin/projctl.git
```

Development install (editable):

```bash
git clone git@github.com:astavonin/projctl.git
pipx install -e ./projctl
```

## Configuration

Configuration is layered by purpose. Config file resolution: `./projctl.yaml` (project-local) then `~/.config/projctl/config.yaml` (user-wide), first found wins.

**Planning-only config.** If you only need `projctl sync`, a single field is enough — the path to your Google Drive mount:

```yaml
planning_sync:
  gdrive_base: ~/GoogleDrive
```

**Ticket and MR config.** For issue creation, MR posting, and everything that touches the platform, add the platform selection, the group or organization scope, the default labels applied to new issues, and the reviewers automatically added to every MR:

```yaml
platform: gitlab

gitlab:
  default_group: "my-group/my-project"
  labels:
    default: ["type::feature", "status::backlog"]
    allowed: ["type::feature", "type::bug", "status::backlog", "status::in-progress"]

common:
  mr_template:
    reviewers:
      - alice
      - bob
```

**Label allowlist.** When `labels.allowed` is set, every command that accepts a label — `create` for issues, `update --add-label` for issues, MRs, and epics, and `create-mr` for merge requests and pull requests — rejects labels outside the list before making any API call. Omit the key to skip the check entirely; set it to `[]` to reject every label.

Run `projctl config` to see which config file is active and what the fully merged, resolved configuration looks like.

---

For command reference, see [CLAUDE.md](./CLAUDE.md).
