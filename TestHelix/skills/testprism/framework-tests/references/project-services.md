# Framework and client contracts

Separate a framework lifecycle requirement from the domain assertion. Reuse the project's configured settings, fixture factories and transport fakes where available; inspect one working neighboring test before building a new harness.

Django's own test runner is `tests/runtests.py`, supports focused labels, and requires a settings module. Model tests additionally need registered apps and appropriate test databases. Prefer the existing runner or the framework-tests preparation procedure; for an independent ORM harness read `references/django-models.md`. A pure utility test need not create a database. [Django unit test guide](https://docs.djangoproject.com/en/dev/internals/contributing/writing-code/unit-tests/).

For asynchronous clients, inspect whether the public operation is awaited and what the local event-loop/transport fixtures provide. Preserve the interface at the transport boundary: a synchronous mock returning a value cannot substitute for an awaited call unless it returns a compatible awaitable. Verify observable responses/state, and close clients/tasks created by the test using the project's lifecycle convention. Python's `AsyncMock` models async call behavior where supported by the installed Python. [Python mocking documentation](https://docs.python.org/3/library/unittest.mock.html).

For OAuth, cryptographic, messaging and cache APIs, use a tiny locally generated or public fixture and the documented boundary. Resolve bytes versus text, serialization, expiry/time and state transitions from the public API and nearby tests. Avoid asserting random ciphertext/signature identity or relying on external service credentials. Do not mock away the operation being tested; substitute the external transport only if the public behavior can still be observed.

For application repositories, identify the owning package and entry point from local metadata. Repository identity alone does not prove a Django, asyncio, Redis or Kafka runtime is available. Establish the requested behavior on the task's provided environment and keep fixture initialization failures distinct from a regression failure.
