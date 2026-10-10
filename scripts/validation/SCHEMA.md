# Task Validator Schema

`scripts/validation/verify_tasks.py` is the golden-standard validator for task legality.

## Validation Contract

A task is `valid` only when all of the following are true:

1. Docker image build succeeds.
2. A fresh pre-solution container can run `run-tests.sh` and the tests fail as expected.
3. A fresh post-solution container can apply `solution.sh` or `solution.yaml`.
4. The same `run-tests.sh` passes after the solution is applied.
5. No infrastructure error interrupts the run.

## Static Contract

The validator requires:

- `task.yaml`
- `docker-compose.yaml` or `docker-compose.yml`
- `run-tests.sh`
- `solution.sh` or `solution.yaml`
- absolute `workdir` when declared; missing `workdir` falls back to `/app`
- `parser_name` to be one of the known `ParserName` enum values

The validator also applies these explicit rules:

- `run_tests_in_same_shell` is recorded for audit, but does not block validation because the validator executes the whole `run-tests.sh` in one shell already
- multi-service compose files must expose a `client` service, otherwise they are classified as `unsupported_multi_container`
- `run-tests.sh --setup` is only invoked when `task.yaml` explicitly sets `run_tests_setup_hook: true`
- missing `tests/` is allowed and recorded in source profiling only

## Result Fields

Each result object in the output JSON contains:

- `task_id`
- `source`
- `source_dir`
- `difficulty_score`
- `verified`
- `verdict`
- `fail_phase`
- `fail_reason`
- `duration`
- `phases`

`verified` is preserved for backward compatibility and is equivalent to `verdict == "valid"`.

## Verdicts

Stable verdict values:

- `valid`
- `invalid_structure`
- `invalid_build`
- `invalid_initial_test_behavior`
- `invalid_solution_behavior`
- `invalid_final_test`
- `unsupported_multi_container`
- `unsupported_solution_format`
- `unsupported_same_shell_semantics`
- `infra_error`

## Phases

The validator preserves the `phases` object for auditability. Current phase keys include:

- `contract`
- `build`
- `up_pre`
- `up_post`
- `setup_pre`
- `setup_post`
- `initial_test`
- `solution`
- `final_test`

`fail_phase` remains the coarse failure bucket used by resume and retry flows.
