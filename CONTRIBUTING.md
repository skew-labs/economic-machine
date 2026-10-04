# Contributing

Start with the [tool index](products/README.md) and [architecture](docs/ARCHITECTURE.md).
Use a source checkout and the installation steps in the root README.

Keep money quantities typed and exact. Preserve reservations on unknown outcomes, bind approval
to the exact plan, and separate candidate generation from signing. Changes to contracts,
settlement or recovery need adversarial tests for the affected boundary.

Run targeted Python tests with `python -m unittest discover -s tests -p 'test_<module>.py'`.
The C++ entry point is CMake; CTest keeps assertions enabled in Release builds. Contract suites
require artifacts from the matching compiler scripts. Old build receipts do not verify new code.

Keep PRs focused: state the observable change, link affected tests and report limitations.
Do not add credentials, private journals, owner configuration, internal roadmaps, test logs or
generated recordings. Archive generated artifacts separately; preserve fixtures explicitly used
by tests. No CI job should receive production signing keys or submit transactions.

Public documentation explains current interfaces, architecture, installation and
verification. It does not contain competition scripts, internal PR plans, meeting
notes, deployment diaries or personal server inventories. `docs/` uses an explicit
Git allowlist; add a reusable technical guide deliberately, not with `git add -f`.
The public-tree checker rejects generated artifacts and internal document names
even if they were force-added. Keep such records in an operator archive outside
the repository.
