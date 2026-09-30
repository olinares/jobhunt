# Kickoff

## One-time setup
1. Create an empty GitHub repo (e.g. `jobhunt`). Unzip this folder into it.
2. Commit and push the scaffold straight to `main` — the only direct commit to `main`.
3. Install the GitHub CLI and run `gh auth login`.
4. In the repo's GitHub settings, protect `main`: require a pull request before merging and require the
   CI check to pass. This makes the PR workflow enforced, not just requested.
5. Open the repo in Claude Code (`cd jobhunt && claude`, or the desktop app).

## Wave 1 prompt (paste into Claude Code)
---
Read CLAUDE.md, PLAN.md, src/jobhunt/models.py and every brief in agents/. Start in plan mode.

We're running Wave 1: agents A–E in parallel, each as a subagent in its own git worktree,
each touching only the files its brief lists. The models.py contract is frozen.
Follow the Git workflow in CLAUDE.md exactly.

Before spawning anything, show me:
1. Any conflicts or gaps between the briefs (file overlaps, missing interfaces).
2. The merge order (A before B).
3. What each agent will record as fixtures and from which real company boards.

After I approve: spawn the five subagents. Each works on the branch named in its brief, runs
ruff and pytest, pushes, and opens a PR with `gh pr create` using the PR template. Don't merge
anything. When all five PRs are open, give me a summary: PR links, CI status, what each agent
flagged, and the recommended merge order.
---

## After you merge Wave 1
Paste: "Wave 1 is merged. Plan Wave 2 per PLAN.md, same Git workflow, one PR." Each later phase works
the same way: plan first, then branches and PRs, and you merge.

## Reviewing a PR with Claude
"Review PR #3 against its brief in agents/. Check it only touches its owned files and flag anything risky."
Fixes go back to the same branch as new commits.
