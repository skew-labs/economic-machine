# Test inputs

The JSON files in this directory are frozen public-testnet inputs and receipts
used to test validation and read-only presentation. `engine-settlement.json`
contains the Engine receipt bundle; `sepolia-workspace.json` is the separate
workspace presentation fixture. They are not current account state, deployment
authority or a production success claim.

Tests inject these paths explicitly. Operator databases, raw provider inputs,
compiler outputs and new runtime evidence remain outside tracked test fixtures.
