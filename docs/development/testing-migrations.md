# Testing and migrations

Use temporary databases in focused tests. The full handoff checks are:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
mkdocs build --strict
git diff --check
```

Every persistent schema change requires the next ordered migration and upgrade coverage from older
schemas. Migrations must preserve existing databases, exact money, quantity, lifecycle, stable IDs,
and non-reuse of Q-numbers. A presentation or documentation change should not need a migration.

Schema v11 adds durable research snapshots. It preserves existing rows, creates no snapshot
automatically, and stores bounded version-1 research payloads with authoritative UTC save times.
Upgrade is automatic through the ordered transaction; downgrade is not supported.

Tests should assert meaningful structure and behavior rather than decorative CSS values. Networked
integrations are mocked unless an explicitly authorized, read-only production smoke is being run.
