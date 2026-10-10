---
name: cli-artifacts
description: "Verify CLI exit status, streams and durable filesystem side effects."
metadata:
  components: [preparation, generation, repair]
---

Invoke the real public entry point with explicit arguments, working directory and controlled environment. Capture exit status, stdout and stderr separately. Check resulting files and metadata when the task promises durable artifacts; a success message alone is insufficient.
Use fixtures under agent-tests/ with independent temporary subdirectories per case. Exercise meaningful combinations such as relative paths, spaces, repeated execution and preexisting files only where supported. Compare parsed structures unless exact byte formatting is part of the contract.
Find the original project root from the runner location and use installed runtimes. Bound subprocesses and clean up descendants and temporary resources. A missing executable, bad invocation or import failure is a harness error. Preserve nonzero assertion status through pipes and cleanup commands.
