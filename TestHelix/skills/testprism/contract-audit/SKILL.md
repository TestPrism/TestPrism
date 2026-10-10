---
name: contract-audit
description: Review regression assertions against public requirements, separating evidenced defects from optional test improvements.
metadata:
  components: [review]
---

Review tests without a source repair. A material finding needs a public requirement or established interface and an observable false acceptance or rejection. Cite numbered repository reads with path and startLine/endLine and numbered original requirements with taskLines. The host extracts exact text; do not transcribe quotations. Check that the cited text actually supports the finding.

Before declaring required behavior absent, inspect the complete relevant test and its assertion helpers. Identify the input, operation and observed output; do not interchange their roles. An assertion can already cover a requirement through a helper or parameterized case. If it is unreachable because of an earlier failure, distinguish reachability from missing assertions.

Blocking defects are broken setup/runner, unsupported assertions or uncovered required behavior. Attribute baseline exception/no-work IDs and executionChecks using the fixture, operation and taskLines. Missing newly required behavior may explain a legitimate baseline failure; a generic setup control cannot validate a subsequently changed fixture. Inspect the full artifact when necessary.

For a blocking unsupported assertion, provide falseRejection: concrete allowed behavior, why it is allowed, assessment of positive/preservation controls, and publicBasis using taskLines or repository lines. Check compatibility in public tests, docs and interfaces. Public support is needed for exact counts, ordering, representations and API choices. Guessed lists still exclude allowed alternatives. Skipping the only required regression leaves a gap.

Inspect assertions after the first failure and their output producer, including runtime escapes and command reporting. A known supported input can validate the observation channel. Optional fixtures, extra adversarial coverage and unsubstantiated risks remain suggestions. Do not infer hidden requirements or broaden an explicit contract into every superficially related operation. Record unresolved interpretations as suggestions rather than inventing evidence.
