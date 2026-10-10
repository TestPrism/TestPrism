# CLI, service and build-system projects

Locate the owning package's build/test entry point from local metadata and a neighboring test. Monorepo root commands can build or initialize unrelated components. Reuse the configured toolchain and pinned dependencies; do not fetch a new compiler or service to match current documentation.

For Go projects, `_test.go` files and `go test` are the standard test mechanism. Identify the module/package boundary and build tags from the checkout before placing tests or selecting a package. Verify that an exported test runner reaches the current source rather than a prebuilt stale binary. [Go testing tutorial](https://go.dev/doc/tutorial/add-a-test).

For Java or native projects, inspect the existing Maven/Gradle/CMake/Make target and smallest relevant test. A test outside the project's discovered source set may compile nothing. A successful command with zero selected tests is not a positive control.

For CLI tools, observe the task's exit status, stdout/stderr and files independently. Use a temporary working directory with explicit configuration where appropriate, and run the actual public entry point. Serialization should be compared as structured data when formatting is not part of the contract. Shell wrappers must preserve the test process's failure status.

For services, establish the lifecycle actually provided by the task: in-process fixture, subprocess or configured local endpoint. Poll a meaningful readiness condition within a bound rather than sleeping a fixed long interval. Keep test-created resources scoped to the test; invoke no unrelated cleanup or infrastructure changes. A command's completion may need a separate internal replay step while exported tests observe its durable result; use the existing artifact-workflow contract for that split.

These are discovery questions for Ansible, AWS CLI, charmcraft, jc, xonsh, yt-dlp, Serverless, Vuls, Flipt, Teleport and Trino. They do not prescribe a shared framework or assert any repository-specific command not verified in this checkout.
