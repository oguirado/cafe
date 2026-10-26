# GITHUB REPO RULE v1.1 — private first, public later, secure from day 0
(v1.1 2026-10-24: desktop/capture products, quick-start wording, agent-playbook pointer pattern; field notes from Café.)

Source: the math-router release (private 2026-10-16, public 2026-10-21) and its two blind reviews. Owner: the operator.
Applies to every repository an agent creates or prepares for GitHub on this host. Agents propose; the operator gives each GO.

## R0. Hard rules
- No `git push`, `gh repo create`, visibility change, release or settings change without the operator's explicit GO naming the act, repo and branch.
- Create PRIVATE first. Only the operator flips to public.
- Commits use the repo-local identity `git config user.name "<handle>"` and `git config user.email "<id>+<handle>@users.noreply.github.com"`.
  Add NO Co-Authored-By or other trailers unless the operator asks; check that the agent's commit template does not add them.
- Never paste secrets, tokens, `.env`, `auth.json` or keyrings into a repo, brief, log or chat.

## R1. Layout: the public tree is built, not cleaned
- The repository folder contains ONLY files meant for publication. Working material lives elsewhere and is never referenced from the repo:
  briefs, worker receipts, logs, fleet or launcher scripts, host paths, campaign files and private policies.
- If a separate working tree already exists, build the release folder by COPYING an explicit allowlist of files. Never "clean" a working tree.
- Machine-specific settings go in an untracked `local/` or `.env.local` (ignored), loaded only if present. Code paths stay repo-relative.
- Agent-playbook pointer pattern: the tracked AGENTS.md / CLAUDE.md holds only public rules and says "if local/AGENTS.local.md exists, read
  it"; the ignored local file points to the orchestrator's playbook, briefs and fleet scripts kept outside the repo.
- Keep a fresh history: one root commit made for publication. Check with `git rev-list --max-parents=0 HEAD`.
  If old history must stay, scan every commit (see R3) before the flip.

## R2. Day-0 files (first commit)
- `README.md` (what it is, a quick start OR install path tested from a clean clone - for desktop apps the honest one is often dev mode,
  e.g. npm/cargo/python - an example output, what is measured vs claimed).
- `LICENSE` (+ `NOTICE` when derived from Apache-2.0 work); `CITATION.cff` if it is research.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/pull_request_template.md` (same commands as CONTRIBUTING),
  `.github/ISSUE_TEMPLATE/bug.md`, `.github/CODEOWNERS`.
- `.gitignore`: caches, venvs, build/dist, `*.so`/`*.o`, renders, logs, `local/`, `.env*`. It is NOT a privacy tool.
- `.github/workflows/ci.yml`, `.gitleaks.toml`, `tools/publication_lint.*`, `tools/claims_check.*` (R3).

## R3. CI gates (every push and PR; least privilege)
- `permissions: contents: read`; pin every action to a full commit SHA; pin downloaded binaries by version AND sha256.
- Tests and lint for every supported runtime version (matrix), and state the minimum version in the README.
- Desktop / capture / audio / GPU products whose real paths need an interactive session (e.g. Windows Graphics Capture, WASAPI, OBS)
  cannot run on hosted runners: run stubbed unit tests on the hosted OS matrix (e.g. windows-latest + ubuntu-latest) and keep a manual
  acceptance receipt per release (machine class, OS build, steps, result, date) committed under docs/acceptance/; the README says which
  paths are CI-tested and which are manually accepted.
- Secret scan: gitleaks over the FULL history (`fetch-depth: 0`).
  - Allowlist only exact identifier shapes as whole-line anchored regexes (`regexTarget = "line"`, `^...$`). Never allowlist a directory.
  - Negative controls in the same job: plant random credentials, which must ALL be detected:
    (a) a `ghp_` token; (b) a 64-hex `secret_key`; (c) a token appended to an allowlisted line.
  - Values containing "EXAMPLE" are ignored by gitleaks, so do not use them as controls.
- Publication lint (public, generic): absolute home paths, user and host names, e-mails other than the declared contact, private-key headers, non-English file names.
  A stricter private deny-list may run locally before release from a policy file kept OUTSIDE the repo, with hashed literals.
- Claims check: every path, tool, flag, CI step and number named in the docs exists or matches its receipt.
- Reproducibility: any published number is re-computed in CI from shipped inputs (byte-compare receipts).

## R4. Local guard (private phase)
- Install `.git/hooks/pre-push` that runs the publication lint, the claims check and gitleaks with the same config; it blocks the push on failure.
  Keep the hook source in `tools/hooks/pre-push` and install it with one documented command.

## R5. GitHub settings
- Private phase (GitHub Pro allows branch protection and rulesets on private repos):
  - Protect `main`: PR required, strict required status checks with the REAL job names (with a matrix: `verify (3.10)`, ...),
    no force-push, no deletion, linear history, conversations resolved.
  - Read the names from `gh api repos/OWNER/REPO/commits/<sha>/check-runs --jq '.check_runs[].name'`. A wrong name blocks every PR.
  - Dependabot: add `.github/dependabot.yml` (github-actions + the package ecosystem), and enable alerts and security updates.
  - Delete head branches on merge; squash or rebase merge only.
- GitHub's own secret scanning and push protection on a PRIVATE repo are a separate paid security product, not part of Pro
  (verify with `gh api repos/OWNER/REPO --jq .security_and_analysis`). Until then, R3 + R4 are the guard.
- At the public flip (operator), then immediately:
  - Enable secret scanning + push protection (free on public repos):
    `gh api -X PATCH repos/OWNER/REPO -f 'security_and_analysis[secret_scanning][status]=enabled' -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'`.
  - Re-apply and verify branch protection.

## R6. Public flip checklist (all green, in this order)
1. Clean-clone test: clone into a temp dir and run every CI command.
2. Privacy: publication lint + private deny-list = 0 findings, except the declared contact. Run gitleaks over the full history.
3. Docs claims check = 0 dead claims.
4. Blind review by two model families with a fixed rubric. Fix the real defects; style suggestions are optional.
5. CI green on the final commit. ONLY THEN tag and create the release (tagging before green means re-pointing tags).
6. Operator flip: `gh api -X PATCH repos/OWNER/REPO -f visibility=public` (older gh lacks `--accept-visibility-change-consequences`).
7. R5 post-flip settings; verify visibility, protection and security settings with `gh api`.

## R7. External agents (web ChatGPT / Gemini / Jules)
- They work on branches and open PRs, and never push to `main`. A PR merges only with green required checks and the operator's approval.
- Pull results back with `gh pr list`, `gh pr checkout <n>`, re-run the gates locally, then merge.

## R8. Do not repeat (math-router lessons)
- A doc describing a design or CI that does not exist (the first review deduction).
- A path allowlist in the secret scanner (it hid real risk), or an unanchored line allowlist (it matched any line containing a hash).
- Required-check names that do not match matrix jobs.
- Tagging before CI was green.
