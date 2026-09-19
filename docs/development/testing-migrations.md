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

Schema v12 adds one optional actual sourcing-travel record per inventory item. It creates no facts
for existing Q-numbers, preserves unknown separately from zero, stores exact scaled decimals, and
restricts ownership to an explicit inventory foreign key.

Schema v13 makes historical acquisition source, date, and cost nullable. The inventory-table
rebuild preserves existing records, identifiers, lifecycle timestamps, marketplace linkage, and
foreign-key relationships. Missing historical facts remain unknown; an explicit zero acquisition
cost remains a known zero.

Schema v14 adds nullable sale-revenue component fields for item revenue, buyer-paid shipping,
marketplace-collected tax, and checkout total. Existing aggregate seller revenue remains
authoritative, while every newly added component stays unknown until an explicit, compatible eBay
re-import safely enriches it. The migration never derives components from totals or rewrites a
historical sale with guessed values.

Tests should assert meaningful structure and behavior rather than decorative CSS values. Networked
integrations are mocked unless an explicitly authorized, read-only production smoke is being run.
