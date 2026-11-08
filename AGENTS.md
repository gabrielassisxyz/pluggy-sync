# pluggy-sync — Agent Briefing

> Read before every interaction. Living spec: short, imperative. On every gotcha or
> decision, append one line here.

> **What it is:** pulls everything a Pluggy (Open Finance) account will hand over into one
> local SQLite file, so the data outlives what the provider keeps.
> **Calibration:** Tier 2 · Phase: work→right. No external users; the stakes are personal:
> the database holds a person's complete financial history and the provider does not
> reproduce past values, so a silent gap or a wrong number is lost for good. Update the
> phase as the project moves `work → right → fast`; an agent reads this line to decide how
> much rigor a change deserves.
> **Review gate:** standard. One independent external opinion over the whole branch diff,
> once, before push.

## Stack & Commands
- **Stack:** Python 3.12+, standard library only. No install step, no lockfile.
- **Layout:** `pluggy_sync.py` is the entry point and the table mapping; the HTTP walk
  lives in `pluggysync/` (`transport.py` fetches bytes and never raises, `walk.py` turns
  them into pages and an outcome). Every attempt reaches `on_attempt` before parsing.
  `ledger.py` holds the ledger schema (append-only by trigger), the file classifier and
  the backup-then-migrate framework; `lock.py` is the one-writer-per-file `flock`, taken by
  the legacy sync too. A legacy `pluggy.db` is never migrated in place.
- **Run:** `./pluggy_sync.py` with `PLUGGY_CLIENT_ID`, `PLUGGY_CLIENT_SECRET`,
  `PLUGGY_ITEM_IDS` in the environment (`PLUGGY_DB` optional). Read-only against the
  API; it writes one SQLite file and exits non-zero when the run is not complete.
- **Modes:** no flag is the ledger sync against `PLUGGY_DB`, which must carry its `cutover`
  row (a legacy file or an unpromoted ledger is refused). `--candidate <path>` runs it against a
  dedicated candidate file (never the live one, never an unignored path in the repo), with
  `--from-states` building the candidate from frozen legacy copies; `--promote <candidate>
  --from-states ...` installs a checked candidate; `--check`, `--rebuild`, `--reparse`.
  `pluggysync/cli.py` owns the dispatch. The legacy sync is gone; its schema lives in
  `pluggysync/legacy_schema.py` for the import and the promotion.
- **Test:** `python3 -m unittest discover -s tests -t .`
- **Gate:** `bin/ci` (secret scan, shellcheck, ruff, compile, tests). It is the exact
  thing CI runs. Run it before a PR.
- **Tracker:** work is tracked with `br` in `.beads/` (`br ready`, `br show <id>`). A bead
  is self-contained: implement from it, not from an external plan.
- **Worktrees:** `bin/worktree new <type>/<kebab-desc>` before the first write; never
  work in the main tree. `bin/install-hooks` once after clone.
- **Merging:** an agent merges its own pull request once `bin/ci` is green locally and
  every check on the PR is green; nobody waits for a human to press merge. Work still goes
  through a branch, atomic commits and a PR whose body carries the why.

## Scope (current)
- **Current scope:** collect every resource the Pluggy API exposes for the configured
  connections into a local SQLite database, keeping the provider's JSON for each row,
  plus the container image that ships the program.
  Don't expand beyond it without a present need; if a change drifts past it, STOP and
  flag it.

## Release
- Cut a version by pushing a `v*` tag. The image workflow builds the `test` stage,
  builds `final` with `PLUGGY_CODE_VERSION=<tag>-<short-sha>`, refuses when the
  version tag already exists in the registry, smoke-tests the image, then pushes
  that tag and no `latest`. Version tags are immutable because the ledger is
  append-only and each run names the code that wrote it.
- `bin/ci` does not build images.

<!-- BEGIN universal-principles v3 -->
## Working principles

- **The human defines the WHAT; the agent decides the HOW.** Plan first for non-trivial
  tasks: show the plan and the to-do list, and wait for approval before building.
- **State assumptions; never pick between readings silently.** If several fit, present
  them. If a simpler approach exists, push back. If the task is impossible as stated, or
  information is missing, say so instead of guessing. This is a bias, not a ritual.
- **Surgical changes — touch only what you must.** Every changed line traces to the task.
  Don't "improve" adjacent code, reformat, or refactor what isn't broken; match existing style
  even if you'd do it differently. Flag unrelated dead code — don't delete it. Remove only the
  imports / variables / functions your own change orphaned.
- **Chesterton's Fence — find the problem before undoing the decision.** A config, a flag, a
  workaround that looks arbitrary is a **fence**: someone put it there, probably to fix
  something that is invisible to you *because the fence is working*. You arrive with no
  history, so absence of a visible reason is evidence of your ignorance, not of its
  uselessness. When your fresh measurement contradicts what the human vaguely remembers
  ("I changed this once, because of some problem"), **your measurement is the suspect first**
  — it may be measuring the case that *isn't* failing. Go find the original problem, then
  decide. *(A CIFS share was benchmarked with a big sequential `dd`, looked fast, and the
  local-disk download dir was "fixed" away — while the actual failure was random writes:
  par2, unrar, torrent piece-writes. Two wrong commits.)*
- **"Flaky" is not a diagnosis — test in the environment the thing actually runs in.** A
  component that fails *consistently* under automation is being **mis-invoked**, not being
  unreliable; "it works when I run it by hand" is not evidence that it works. The shell you
  test in has a TTY, a `$HOME`, an `ssh-agent`, an interactive stdin — the systemd unit, the
  CI job and the scripted harness have none of those, so a passing manual run can be testing
  a different program. Reproduce it *there* (start the unit, `env -u SSH_AUTH_SOCK`,
  `</dev/null`, `--dry-run` to print the real command line) before accepting "unstable" as a
  cause. **When a fix doesn't change the symptom, stop fixing and go look at what is actually
  being executed.** *(An interactive-mode flag with no TTY made one harness fail every review
  panel for weeks, written off as "flaky"; it was the wrong flag.)*
- **KISS and YAGNI — don't build for a need that doesn't exist.** No preventive
  abstractions, no single-use interfaces. Architecture is *extracted* once a pattern proves
  itself in real use, never designed up front. Most over-engineering is doing "right" or
  "fast" before a working thing justifies it. When a solo tool starts being framed as a
  public / multi-tenant / plugin-system / configurable-N-backends platform with no present
  need, STOP and ask — that is a standing duty, not a suggestion.
- **Development cost is not your cost — don't let it pick the design.** Choosing between
  technical options, weight quality, simplicity, robustness and long-term maintainability;
  don't weight how long the work takes. The estimate comes out in human units — days,
  weeks — because that is what the training data measured, and the cheaper option then
  wins on a cost the agent does not pay. This is **not** licence to over-build: KISS and
  YAGNI decide *whether* a thing is needed, and this decides *how well* it is built once
  it is. "That would take a week" is not an argument here; "nothing needs this yet" is.
- **No silent decisions (comprehension debt).** Never make a silent architectural or
  design call — state it and record the rationale, so the reasoning is recoverable later.
- **Real decisions go in the chat, on their own — never via a popup.** Surface the options,
  what each means, the trade-offs, and a recommendation; then decide together. Don't bury
  it mid-text or bundle it with other topics, and don't compress it into a quick-pick widget
  (e.g. AskUserQuestion) — the widget overlays the reasoning instead of showing it. Widgets
  are for trivial short-answer picks only.

## Git: branches, commits, PRs, comments

- **Ask the repo for its default branch; never assume one.** Repos differ — `master` and `main`
  are both common, often in the same person's account — and a wrong guess sends a PR to a branch
  that does not exist, or, worse, has you "fixing" a URL that was right all along.
  `git symbolic-ref --short refs/remotes/origin/HEAD | sed 's|^origin/||'`, or
  `gh repo view --json defaultBranchRef -q .defaultBranchRef.name`.
  Never commit directly to it: branch, then PR.
- **A new repo starts on `main`.** That is the preferred name, and `init.defaultBranch` is
  set to it, so `git init` produces it without anyone choosing. It settles new repos only:
  an existing one keeps the branch it has, because renaming breaks open PRs, CI filters,
  deploy hooks and every permalink into the tree, and buys nothing. The rule above still
  governs everything already in existence — ask, never assume.
- **Branches** — Conventional Branch (conventionalbranch.org): `<type>/<kebab-description>`,
  types `feature/`, `bugfix/`, `hotfix/`, `chore/`, `release/`, `docs/`.
- **Commits** — Conventional Commits (conventionalcommits.org): `<type>(scope): <description>`,
  types `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, `build`, `perf`, `style`.
  Breaking change → `!` after the type or a `BREAKING CHANGE:` footer.
- **Atomic commits** — one logical change per commit, each independently green and
  revertible. Never `git add .` blind; split unrelated changes.
- **Always work in your own worktree — mandatory, not conditional.** Parallel sessions
  are opened freely and nothing signals their existence to you, so a "check whether another
  session is here first" step can never be reliable — the honest answer is always "maybe".
  The only collision-proof arrangement is structural: keep the main working tree on the
  default branch as a clean reference and **never work in it** — before your first write
  (commit, branch, rebase, stash; read-only exploration is exempt), create your own worktree
  and do everything there. **When the repo ships a worktree tool, that tool is the only correct
  way to make one** — `bin/worktree new <type>/<kebab-desc>`, or whatever the repo calls it.
  A raw `git worktree add` materialises tracked files and nothing else, so every git-ignored
  path the repo links into a worktree is silently absent, and the tools that read those paths
  fail inside it without saying why: a tracker whose database is git-ignored reports itself
  uninitialised and offers to create a second one. Only where the repo ships no such tool is
  `git worktree add ../<repo>-<task> -b <your-branch> <origin>/<default-branch>` the right
  command. Do this **whether or not** you believe another agent is running — that belief is
  exactly what you cannot verify. Report which worktree/branch you used; remove it once merged.
  Only the human can see all the open sessions.
- **Pull requests** — describe **what + why**. *What*: a 1–3 line summary. *Why* (the bulk):
  decisions, trade-offs, rejected alternatives. The diff shows the what; the PR explains why.
- **Comments** — always **WHY, not WHAT**: explain intent, never restate the obvious
  mechanics. Keep existing comments; they carry intent.

## Code style (baseline)

- Functions: 4–40 lines, one thing each (SRP). Files: under ~500 lines, split by responsibility.
- Names specific and unique — avoid `data`, `handler`, `Manager`, `util`.
- Explicit types. Early returns over nested ifs; max ~2 levels of indentation.
- Inject dependencies; wrap third-party libs behind a thin interface this project owns.
- No duplication — but don't extract *too early*. Tolerate duplication while the pattern is
  still forming; extract the abstraction *from* proven, repeated code, never ahead of it.
- **Refactoring is not automatic.** After a large feature, list the candidates and ask
  before pruning; consolidate once the thing works and the seams are obvious, not before.
<!-- END universal-principles v3 -->

## Git & secrets
- Before any commit, show `git status` + `git diff --cached`; confirm no secret is staged.
  If you spot one, STOP and report it. The gitleaks pre-commit hook is the deterministic
  backstop; this habit is the probabilistic one.
- Real secrets stay out of git. The database (`pluggy.db`, `pluggy.db-*`) holds personal
  financial data and is ignored; so is every frozen copy of it. Nothing derived from real
  data is committed: test fixtures are synthetic.

## Tests (TDD)
- Every feature is born with a test; every bugfix with a regression test.
- Tests run with ONE command, no manual setup, no credential. If it can't run headless,
  it's wrong.
- The API is faked at the transport boundary with a named fake, not an inline stub.
- Fixtures are built from synthetic literals; a recorded response may inform a shape but
  its values are never copied. A real identifier in a fixture cannot be removed from
  history once pushed.
- Before saying "done", run the tests and `bin/ci`; report the result.

## Small releases
- Every commit on `main` passes `bin/ci` and is runnable; no "broken commit I fix in the
  next one".
- Closed work is committed before switching tasks; flag it if it has not been.

## Security (habit, not a phase)
- When touching the HTTP client, the environment, or the database file, flag the risk and
  propose the guard. The API key travels only in a request header and is never logged or
  stored.
- Files the program creates are private (`0600`); the database is personal data.
- No third-party dependency means no dependency CVE scan; if one is ever added, add the
  scan to `bin/ci` in the same change.

## Post-implementation checklist (run before "done")
1. Commits small and well-described.
2. Refactoring candidates listed (if the change was large).
3. Security risks flagged (if you touched a sensitive surface).
4. This spec updated if behavior, setup, or release flow changed, and any hurdle it
   gained is classified, not just appended.

## Common hurdles

| hurdle | class | gate |
|---|---|---|
| Pluggy publishes no endpoint that lists connections; an item id not in `PLUGGY_ITEM_IDS` is invisible | prose | none |
| `PATCH /items/{id}` (refresh on demand) is refused on the MeuPluggy connector; freshness is the provider's own daily sync | prose | none |
| Python 3.12+ `unittest` exits 5 when no test ran; an empty `tests/` is a red gate, not a green one | ci | `bin/ci` |

**A hurdle promoted to a gate is deleted from this table, not duplicated.** The gate is the
instruction; a line here restating it only dilutes the ones still unguarded.
