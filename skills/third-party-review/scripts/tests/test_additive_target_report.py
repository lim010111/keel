#!/usr/bin/env python3
"""Mode-report regression test for prepare_review.py main() in ADDITIVE mode.

A third-party-review run in *additive* mode means the user explicitly passed one
or more --target paths ALONGSIDE the default transcript review (pinned evidence
to be read in full and used to judge soundness). The contract under test is the
faithfulness of the printed `mode` field to what was *requested*:

    When the user passes --target (additive intent) but the target does not
    resolve, main() keys `mode` on the RESOLVED targets (`entries`), not on the
    REQUESTED ones. With zero entries it computes `mode: "transcript"` — the
    bare string used when NO target was ever passed — and exits 0. The only
    trace that a target was requested is a separate `errors` field.

The reviewers' minimal ask: never report `mode` as the bare string `transcript`
when --target was passed. Reporting `transcript` actively MISDESCRIBES the
request: a downstream reader of the `mode` field alone cannot distinguish "no
target asked for" from "target asked for but silently dropped". SKILL.md's only
guard is an instruction telling the main agent to read `errors` and stop — an
LLM-discipline mitigation, not enforcement, and it does not license the `mode`
field itself misreporting what was requested. The cost of the miss is a paid
multi-model run that never looked at the pinned file.

This test drives the real script via subprocess from a throwaway temp project,
fabricating a tiny valid session .jsonl so the transcript pipeline runs without
a real session, and passing a missing --target. It asserts the reported `mode`
is NOT the bare string `transcript`. On HEAD (17dd045) the script prints
`mode: "transcript"`, so this test FAILS.

Hermetic (builds its own fixtures under a temp dir, never touches ~/.claude),
re-runnable, self-cleaning (no .tpr left behind), stdlib-only. Run with
`python3 <thisfile>`. Exit 0 = mode faithfully reflects the additive request
(pass); exit non-zero = the additive request was misreported as transcript
(fail).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prepare_review.py",
)

# A minimal valid session JSONL: one human turn + one assistant turn. No
# last-prompt/leafUuid entry, so the script falls back to the linear transcript
# (path_ok=False) — that is fine; we only need the transcript pipeline to run.
TINY_JSONL = (
    '{"type":"user","uuid":"u1","parentUuid":null,'
    '"message":{"role":"user",'
    '"content":[{"type":"text","text":"please review my work"}]}}\n'
    '{"type":"assistant","uuid":"a1","parentUuid":"u1",'
    '"message":{"role":"assistant",'
    '"content":[{"type":"text","text":"here is what we did"}]}}\n'
)


def main():
    if not os.path.isfile(SCRIPT):
        print(f"FAIL: prepare_review.py not found at {SCRIPT}")
        return 2

    root = tempfile.mkdtemp(prefix="tpr-additive-report-")
    try:
        proj = os.path.join(root, "proj")
        os.makedirs(proj)
        jsonl = os.path.join(proj, "tiny.jsonl")
        with open(jsonl, "w", encoding="utf-8") as fh:
            fh.write(TINY_JSONL)

        missing = os.path.join(root, "nonexistent", "typo.md")  # never created

        cp = subprocess.run(
            [sys.executable, SCRIPT, "--jsonl", jsonl, "--target", missing],
            cwd=proj, capture_output=True, text=True,
        )

        detail = (f"exit={cp.returncode} "
                  f"stdout={cp.stdout.strip()!r} stderr={cp.stderr.strip()!r}")

        if cp.returncode != 0:
            # Either a hard-fail or a non-transcript mode would satisfy the
            # invariant; a crash is not what we expect, so surface it loudly.
            print(f"[unexpected] script exited non-zero: {detail}")
            print("PASS: additive request was not reported as bare 'transcript' "
                  "(script did not silently downgrade).")
            return 0

        try:
            out = json.loads(cp.stdout)
        except json.JSONDecodeError:
            print(f"FAIL: could not parse script stdout as JSON: {detail}")
            return 1

        mode = out.get("mode")
        print(f"observed mode={mode!r}; {detail}")

        # The invariant: --target WAS passed, so the request was additive. The
        # `mode` field must not claim the bare transcript-only path.
        if mode == "transcript":
            print()
            print("FAIL: --target was explicitly passed (additive intent) but "
                  "the missing target was silently downgraded — main() reported "
                  "mode='transcript' (the no-target-requested value) and exited "
                  "0. The mode field misdescribes the request; the only trace "
                  f"is errors={out.get('errors')!r}, whose consumption SKILL.md "
                  "delegates to main-agent discipline (not enforcement). "
                  "main() must not report bare 'transcript' when --target was "
                  "passed.")
            return 1

        print()
        print(f"PASS: additive request faithfully reported (mode={mode!r}), not "
              "the bare 'transcript' value.")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
