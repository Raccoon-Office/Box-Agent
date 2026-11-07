---
name: cache-probe-method
description: Synthetic method for the staged context-loading test, used only when explicitly requested.
---
# Staged verification method

This method applies only to the active synthetic test. It operates on files in the task workspace and grants no additional tools or permissions. It requires no research, dependencies, code execution, sub-agents or external services.

After receiving this body, the next response must call write_file once to create skill-proof.txt. Its entire content must be the following receipt followed by one LF newline:

{{SKILL_RECEIPT}}

The receipt is present only in this body. Do not read it from another file, substitute an explanation or claim success without a successful tool result.

Then call read_file once for skill-proof.txt and verify the exact receipt. Each model step makes one tool call. Preserve unrelated files. Report actual failures without inventing receipts.

After readback, resume phase-2.txt: read phase-3.txt and follow its remaining steps. Do not load this method again or start unrelated work.

The receipt is synthetic test data, not a credential. It establishes that this body became visible after get_skill. It does not measure cache reuse. Only response usage from the real provider can establish cache reads; do not estimate or report cache values yourself.
