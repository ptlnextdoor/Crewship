# Forge PR audit, 2026-10-23

Scope: the open PRs against `forge` on ptlnextdoor/Crewship. The request said 7 PRs, but 10 are open: #8–#10 were opened between 14:05 and 16:04 UTC. I audited all 10 because #8 and #10 conflict textually with earlier PRs. Base: `forge` @ d2b2b8ea. In each `file:line`, added lines are numbered as in the PR head and existing code as in base `forge`.

Evidence used: the PR diffs; `forge/forge.py`, `forge/gpu.py` and `forge/projects/reflex1.toml`; the reflex1 session logs under `~/.local/state/forge/reflex1/{,lane*/}sessions/`; the reflex1 tickets and git log. The session logs hold 27 reviewer runs: 19 APPROVE, 3 REJECT, 4 `TIMEOUT after 1800s` with no `VERDICT:`, and 1 Codex usage-limit tail. Throwaway checks are named in the findings that rely on them.

## PR #1 forge builder: existing code takes precedence over stale docs
Verdict: MERGE WITH CHANGE
Risk: low
What it changes: Adds one sentence to step 1 of `prompts/builder.md`. When AGENTS.md, LEARNINGS.md "or any other documentation" disagrees with the code, the builder follows the code's conventions, lists the mismatch in LEARNINGS.md, and does not rewrite working code to match a document.
Findings:
- low forge/prompts/builder.md:5 "any other documentation" also covers the ticket. A ticket is a document whose purpose is to change working code: reflex1 0014, 0020 and 0027 all change existing tests or the forward pass. "Do not rewrite working code to match a document" therefore contradicts "Do exactly this ticket". STEERING.md is not at risk, because forge.py:186 prepends it with "these override anything below". If the builder declines a ticket, it produces no diff, and `fail()` (forge.py:413) burns an attempt.
Change needed: Replace builder.md:5 with:
```text
   When `AGENTS.md`, `LEARNINGS.md` or other docs (not the ticket) disagree with the existing code, follow the conventions used in the existing code, and list each such inconsistency in your `LEARNINGS.md` entry at the end; do not rewrite working code to match a doc.
```

## PR #2 forge: reviewer rejects diffs that remove or silence safeguards the ticket did not name
Verdict: MERGE
Risk: low
What it changes: Adds reviewer check 4, "Removed safeguards", in `prompts/reviewer.md`. The old checks 4 and 5 become 5 and 6, and the reject rule now covers steps 1–5.
Findings:
- No issues; the change only makes the reviewer stricter. Nothing in forge.py, the README or the other prompts refers to reviewer step numbers (grep `steps? [0-9]` in forge/ finds only history in SCOUT.md). The cost is extra REJECTs, capped by `max_attempts = 3`.

## PR #3 forge builder: no sub-agents for code changes on a ticket
Verdict: MERGE
Risk: none
What it changes: Adds one builder rule: no sub-agents that edit code or run the gate, though a read-only search sub-agent is allowed. `builder_hard` gets the rule too, because it uses the same prompt.
Findings:
- nit forge/prompts/builder.md:9 The `(arXiv 2610.10263)` citation is written for humans and does nothing for the builder. Dropping it is optional.

## PR #4 forge: gate-repair pass sees the ticket, not only the gate output
Verdict: MERGE
Risk: none
What it changes: The gate-repair prompt (forge.py:423) now includes `Ticket (<path>):` and the ticket text before the gate output. The comment no longer says the repair is "in-session"; it says correctly that it is a fresh session.
Findings:
- No issues. The ticket text goes into one argv element through a Popen list with no shell (forge.py:179-189), so there is no injection path. The prompt stays small: the ticket is at most 7.9 KB today plus up to 4 KB per forge note, and the gate tail is at most 5 KB. The builder prompt already carries the same ticket text, so there is no new exposure.

## PR #5 forge: reviewer reads builder's notes last, as claims
Verdict: MERGE WITH CHANGE
Risk: medium
What it changes: Lines 432-438 of forge.py split `git diff main...HEAD` in two: a code diff whose pathspec excludes the root PROGRESS.md and LEARNINGS.md, and a separate notes diff of those two files, capped at 8000 characters and shown as `{NOTES}`. `reviewer.md` labels the notes as claims and puts them last. Step 3 now says "numbers in the builder's notes".
Findings:
- medium forge/prompts/reviewer.md:20 The line "a claim your own checks did not confirm is a reason to reject" changes the standard from "a number with no command behind it" (step 3) to "re-run every claim".
  - **Timeouts.** The reviewer has a hard-coded 30-minute limit (forge.py:435) and already timed out with no VERDICT in 4 of 27 runs (lane2 00005 and 00006, lane3 00005, lane4 00002).
  - **GPU claims.** builder.md:11 requires the builder to put the command next to each number. On a GPU ticket, the notes therefore hand the reviewer the pod command behind each number. For example, reflex1 0025 puts the `eval_pod.json` numbers and the `gpu.py list` spend in PROGRESS.md.
  - **Outcome.** The reviewer cannot confirm GPU numbers within 30 minutes, so it either rejects or rents a GPU itself. A reject throws away the pod run and the next attempt rents another pod. The reviewer can rent one: its prompt sets no GPU limit and its env carries `GPU_BUDGET_USD`.
  - **Billing.** If the 30-minute kill hits gpu.py, the SIGTERM skips its cleanup (`finally: terminate()` at gpu.py:153), because gpu.py installs no signal handler. The pod then bills until its deadline. `reap` records it as $0, because API pods carry no `_started` (gpu.py:81-84), so the budget ledger undercounts.
- low forge/forge.py:438 argv size. The whole prompt is passed as one argv element, so Linux's MAX_ARG_STRLEN of 131072 bytes applies. I checked: `subprocess.run(["/bin/true", "x"*131072])` raises E2BIG. Before this PR, the worst case was about 130.6 KB: a 120000-character diff, the 1.9 KB template, the largest ticket (7.9 KB) and STEERING (0.9 KB). This PR adds up to 8000 characters of NOTES without lowering the 120000-character DIFF cap, which brings the worst case to about 138.9 KB. Over the limit, the agent fails to start with an `OSError` and rc 127 (forge.py:204). With no VERDICT, `fail()` burns the attempt.
- low forge/forge.py:438 Notes longer than 8000 characters are cut with no marker. Every later builder and planner session reads PROGRESS.md and LEARNINGS.md (builder.md:4, planner.md:3), so text past the cut reaches main without review. Before this PR, those files sat near the top of the 120000-character diff, because git sorts upper-case paths first.
- I verified the pathspec split on a throwaway repo: `sub/PROGRESS.md` stays in the code diff, and only the root PROGRESS.md and LEARNINGS.md go to the notes.
Change needed:
- forge/forge.py:438: change `DIFF=diff[:120000]` to `DIFF=diff[:112000]`.
- forge/prompts/reviewer.md:20: replace the line with:
```text
Builder's notes (claims, read last; first 8000 characters, the rest with `git diff main...HEAD -- PROGRESS.md LEARNINGS.md`). A number with no command behind it, or a claim your own checks contradict, is a reason to reject; never start a GPU job to check a claim:
```

## PR #6 forge: one repair round after a reviewer REJECT before giving up the attempt
Verdict: MERGE WITH CHANGE
Risk: medium
What it changes: Lines 435-453 of forge.py wrap the review in `for round_ in range(2)`. On any result other than APPROVE, the PR runs one builder repair with the ticket and the review, then commits, pushes, gates, checks protected paths and runs a fresh review. The README flow and safeguard table are updated to match.
Findings:
- medium forge/forge.py:436 The second review cannot see the first. It gets `TICKET=ticket.read_text()`, and the ticket file does not change during the round. Today, a REJECT is written into the ticket by `fail()` → `set_status(..., why)` (forge.py:481), so the next attempt's reviewer reads it in `{TICKET}`. With this PR, a defect the builder only papered over gets a second, independent reviewer instead of a targeted re-check. A throwaway fake-driven run of the PR's `cycle()` confirmed this: in the reject→approve case the reviewer was called twice, and the first review appeared in neither prompt.
- medium forge/forge.py:440-442 Any result other than APPROVE triggers the repair, including a timeout or quota tail with no `VERDICT:`; 4 of the 27 reviewer runs were `TIMEOUT after 1800s`. The builder then gets a 75- or 90-minute session (reflex1.toml `timeout_min`) whose "review" is just a kill message. That is followed by a gate run and another 30-minute review. In the fake run, a single `TIMEOUT after 1800s` reply led to a builder repair and a second reviewer call.
- low forge/forge.py:434 `reviewer` is chosen once, before the loop. Suppose the repair builder falls back to another vendor: `builder`'s fallback chain ends in `openai-codex/gpt-6.1-sol`. The second review still goes to `reviewer_alt`, also Codex, so Codex reviews Codex's code. That breaks the cross-vendor rule in README:29. `done_note` (base forge.py:447) also keeps the pre-repair `built_by`. The fake run confirmed this: with the repair recorded as `openai-codex/sol`, the second review went to `reviewer_alt`.
- low forge/forge.py:449-452 The gate runs before the protected-path check, so a repair that edits gate.sh runs the edited gate. The PR comment then says PASS, but the fail reason says "gate failing after review repair". The next attempt reads that wrong reason in the ticket.
- nit forge/forge.py:444 The comment says "in-session repair round". It is a fresh `--no-session` builder, the same mistake that #4 fixes for the gate repair.
- This PR conflicts textually with #5 (the same reviewer call) and #8 (the same comment lines). There is also a semantic conflict: the `diff` inside the loop must use #5's split and pass NOTES, or the second review gets a literal `{NOTES}`.
Change needed: Rebase onto #5 and #8 with their changes applied. Then replace everything from #5's `# The builder's PROGRESS.md/LEARNINGS.md hunks...` comment down to the line before `# land:` with:
```python
            prior = ""
            for round_ in range(2):
                # The builder's PROGRESS.md/LEARNINGS.md hunks are its own claims; they go to the reviewer separately and
                # last, so the diff is judged before the self-report (arXiv 2610.29609: judges shown the agent's claim miss more misreports).
                diff = self.sh("git diff main...HEAD -- . ':(exclude)PROGRESS.md' ':(exclude)LEARNINGS.md'", cwd=wt).stdout
                notes = self.sh("git diff main...HEAD -- PROGRESS.md LEARNINGS.md", cwd=wt).stdout
                # Reviewer comes from the other vendor than the model that actually built it (cross-family review).
                built_by = self.last_model.get(builder, self.cfg["roles"][builder]["model"])
                same = built_by.split("/")[0] == self.cfg["roles"]["reviewer"]["model"].split("/")[0]
                reviewer = "reviewer_alt" if same and "reviewer_alt" in self.cfg["roles"] else "reviewer"
                rc, rout = self.agent(reviewer, self.prompt("reviewer", TICKET=ticket.read_text() + prior, DIFF=diff[:112000], NOTES=notes[:8000] or "(none)"), wt, timeout_min=30)
                verdict = re.findall(r"VERDICT:\s*(APPROVE|REJECT)", rout)
                review = rout.split("[stderr]")[0].strip()
                self.pr_comment(self.pr, f"**review of `{self.rev(wt)}` by `{self.last_model.get(reviewer, self.cfg['roles'][reviewer]['model'])}`**\n\n{review[-12000:]}")
                if verdict and verdict[-1] == "APPROVE":
                    break
                if round_ or not verdict:  # a timeout or quota tail has no findings to repair
                    return self.fail(ticket, f"reviewer did not approve.\n{rout[-3000:]}")
                # one repair round with the review, then a re-review that is shown this review (round cap, not open loop)
                self.agent(builder, f"The reviewer rejected the change. Fix every instance of each problem the review names (never the gate or tests' expectations unless the ticket says so), run `{self.cfg.get('gate', './gate.sh')}`, then commit.\n\nTicket:\n{ticket.read_text()}\n\nReview:\n{review[-12000:]}", wt)
                self.sh("git add -A && git commit -qm 'forge: repair after review' || true", cwd=wt)
                self.sh(f"git push -q origin {branch}", cwd=wt, timeout=120)
                bad = self.touched_protected(wt)
                if bad:
                    return self.fail(ticket, f"review repair edits protected gate/ticket paths {bad}; not allowed.")
                ok, gout = self.gate(wt)
                self.pr_comment(self.pr, f"**gate after review repair at `{self.rev(wt)}`: {'PASS' if ok else 'FAIL'}**\n\n```\n{gout[-3500:]}\n```")
                if not ok:
                    return self.fail(ticket, f"gate failing after review repair.\n{gout[-2500:]}")
                prior = f"\n\n## Previous review of this branch (REJECT): confirm each finding is fixed\n{review[-3000:]}"
```
I applied this block to a scratch merge of #4, #8, #7, #2, #9 and #5, and `py_compile` passed. In the fake-driven `cycle()`, every case behaved as intended:
- approve → merged;
- reject→approve → the second review prompt contains "Previous review";
- TIMEOUT → fail with no repair;
- reject→reject → fail;
- repair by a Codex fallback → second review by `reviewer`.

## PR #7 forge: agents are told their session clock and to timeout long commands
Verdict: MERGE WITH CHANGE
Risk: medium
What it changes: `_agent_once` (forge.py:176-181) prepends a "Session clock" paragraph to every role's prompt: planner, builder, reviewer, steward and the conflict-merge builder. The paragraph gives the minutes until the kill, says to prefix long commands with `timeout`, forbids foreground watchers and servers, and says "finish (commit, notes, verdict)". STEERING.md still comes first.
Findings:
- medium forge/forge.py:180 "finish (commit, notes, verdict)" goes to every role.
  - **Reviewer.** Its prompt says "Do not edit files" (reviewer.md:1), and now it is also told to commit. A reviewer commit in the builder's worktree is still on HEAD when forge rebases, re-gates and merges (forge.py:442-457), so it lands with no review.
  - **Planner and steward.** forge's path filter only looks at uncommitted files: the `git add -A tickets PROGRESS.md ROADMAP.md` step (forge.py:273) and `discard_strays` (forge.py:495-500). A commit the agent makes itself is pushed unchanged by `push()`.
  - **Today.** The planner already makes its own commits (reflex1 9b64280 and 065080b, so far only in its allowed paths). This text tells every role to do the same.
- medium forge/forge.py:179 "Prefix any command that can run long with `timeout`" also covers `gpu.py run`. builder.md:12 routes all GPU work through it, and reflex1 0025 runs `--hours 1.5` inside builder_hard's 90-minute session.
  - **SIGTERM.** coreutils `timeout` sends SIGTERM, and gpu.py installs no handler (the file never mentions `signal`). Its `finally: terminate(pod)` (gpu.py:153-154) therefore never runs.
  - **Billing and ledger.** The pod keeps billing until `reap`, which runs on a 10-minute timer, passes its deadline. `reap`'s `terminate()` then records `usd: 0`, because API pods carry no `_started` (gpu.py:81-85). `spent()` undercounts, so `GPU_BUDGET_USD` no longer works as a real cap.
- low forge/forge.py:178 "this session and every process it started are killed" is not guaranteed. In this same omp build (18.12.6, run with `-p --no-session --approval-mode=yolo`), each tool command runs as its own session leader: I observed `sh` with pid=pgid=sid 1743619 under omp's sid 1733950. `os.killpg(p.pid, …)` (forge.py:194/197) therefore reaches tool processes only if omp forwards the signal. The `timeout` advice is the useful part, so drop the promise.
- nit forge/forge.py:176 The code comment "(7 of the first 63 reflex1 sessions did)" is a statistic that will go stale.
Change needed: Replace forge.py:176-181 with:
```python
        # Sessions die at the limit, so the agent is told the clock and how not to hang on one command.
        prompt = (f"Session clock: this session is killed after {timeout_min} minutes. "
                  "Prefix any command that can run long with `timeout` (for example `timeout 900 cargo test --workspace`), "
                  "except `gpu.py run`, which enforces its own `--hours` and skips its pod cleanup if killed. "
                  "Never start a watcher or server in the foreground, and finish before the limit.\n\n"
                  f"{prompt}")
```

## PR #8 forge: protected-path check and gate evidence on the exact revision that merges
Verdict: MERGE WITH CHANGE
Risk: low
What it changes: After `rebase_onto_main`, the PR adds a protected-path check against `origin/main`, using a new `base` parameter on `touched_protected` (forge.py:265). The output of the re-gate is now posted to the PR. Every gate and review comment carries the short HEAD sha from a new `rev()` (forge.py:269). The README safeguard row is updated.
Findings:
- This closes a real gap. Before, the builder's conflict-resolution merge (base forge.py:325-331) could edit `gate.sh`, and the re-gate (base forge.py:444) would run the edited gate. Now the attempt fails before the gate runs (forge.py:449-451). Using `origin/main` is correct: local `main` stays at the claim commit for the whole cycle, so `main...HEAD` after the rebase would also pick up other lanes' done-commits in `tickets/`.
- low forge/README.md:30, forge/forge.py:445-446 and forge/forge.py:453 claim the gate runs "on the exact revision that merges" or on the "merged revision". It actually runs on the rebased PR head, and both merge paths then create a new commit:
  - `gh pr merge --merge` (base forge.py:379) merges into whatever main is at that moment.
  - The fallback `git merge --no-ff` (base forge.py:453) merges into a freshly synced main.

  With 4 lanes landing, main moves during the gate, which can take up to 30 minutes. reflex1's main contains both kinds of commit ("Merge pull request #25", "forge: land 0026"). The PR record would therefore label a sha as "merged" when it was not the merged revision.
Change needed:
- README.md:30 becomes:
```text
| Rebase, re-check protected paths and re-gate the updated branch before merge; every gate and review comment on the PR names its commit | green branch, red main; a conflict resolution that edits the gate |
```
- forge.py:445-446 become:
```python
            # land: bring up to date with main, re-check protected paths and re-gate the updated branch (a builder conflict
            # resolution is new code the reviewer never saw), then merge
```
- forge.py:453: replace the label `gate on merged revision` with `gate after update from main at`.

## PR #9 reviewer: rerun reported numbers with a perturbed input
Verdict: MERGE WITH CHANGE
Risk: medium
What it changes: Adds a sentence to reviewer step 1. For any speed, accuracy or agreement number, the reviewer reruns with something the command does not fix (seed, batch size, input set, fresh process) and reports both runs. A number that holds only on the builder's exact input counts as faked. A before/after pair only counts if both runs used the same build profile on the same machine.
Findings:
- medium forge/prompts/reviewer.md:4 These perturbations produce false REJECTs:
  - **Speed.** Latency and throughput change with batch size by design. The box is also shared by 4 lanes, so any rerun differs: reflex1 0014 measures user CPU, "not wall: the box is shared", and 0019 says to "run it twice and keep the lower wall_s".
  - **Accuracy.** Accuracy on another input set also differs for real reasons: 0025's floors are defined only for the holdout episode `summer1_s16`.
  - **Result.** "Holds only for the builder's exact input is faked" turns this normal variance into REJECTs, each costing one of the 3 allowed attempts.
- medium forge/prompts/reviewer.md:4 The rule doubles the slowest reviewer commands: reflex1's "Done when" benches take 3–8 minutes (0012, 0025 step 5). They run inside the hard-coded 30-minute reviewer limit (forge.py:435), which already expired in 4 of 27 runs, and a timeout has no VERDICT, so the attempt fails. Nothing restricts the extra run to CPU.
Change needed: Replace the added sentence on reviewer.md:4 with:
```text
For an accuracy or agreement number, also run the command once on an input it does not fix (another reference case, episode or seed) when that takes a few minutes on CPU, and report both; a result that collapses off the builder's exact input is faked. A before/after speed pair counts only if both sides come from the same build profile on the same machine. Never start a GPU job for these checks.
```

## PR #10 forge builder: reuse an existing implementation instead of adding a second code path
Verdict: MERGE
Risk: low
What it changes: Adds a builder rule: before adding a function, type, flag or data path, search with `rg` and `git log -S` for an existing one and extend it. If the ticket's feature already exists, note the file and symbol in PROGRESS.md and stop.
Findings:
- No issues. Stopping leaves a diff that touches only PROGRESS.md. forge treats it like any other diff (forge.py:412): it opens a PR, runs the gate, and the reviewer runs "Done when". reflex1's planner already writes this exit into tickets (0020: "implement nothing, and set the ticket done"). `rg` is installed (/usr/bin/rg).

## Order
All ten are approved: four as they are, six after the changes above. Merge in this order:
1. #4
2. #2
3. #3
4. #8, after its change
5. #7, after its change
6. #1, after its change
7. #9, after its change
8. #5, after its change; rebase on #2 first
9. #6, after its change; rebase on #5 and #8 first (the change block is written against that state)
10. #10; rebase on #3 first

Textual conflicts: I merged each of the 45 pairs onto `forge` d2b2b8ea in a scratch clone. 41 pairs merge cleanly, and the order above conflicts only at #5, #6 and #10. The four conflicting pairs:
- **#2 × #5** in `forge/prompts/reviewer.md`, steps 3–6. Keep #5's step 3 wording ("numbers in the builder's notes") and #2's steps 4–6.
- **#3 × #10** in `forge/prompts/builder.md`. Both insert a bullet after line 8; keep both.
- **#5 × #6** in `forge/forge.py`, at the reviewer call. #6's change block resolves it.
- **#6 × #8** in `forge/forge.py`, at the review comment and the lines after the review. #6's change block resolves this one too.

Cross-PR note: #2, #5 and #9 all add reviewer work under the same fixed 30-minute reviewer limit (forge.py:435). #7's clock line is the only change that tells the reviewer what budget it has.
