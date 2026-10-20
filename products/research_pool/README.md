# Muse research pool

A persistent dispatcher connects the pinned `meta/muse-spark-1.3-contributor`
model to evidence jobs from Solana, Arbitrum One and Robinhood Chain. Up to fifty
client requests can be in flight. This is client concurrency, not a guarantee
about provider execution or a throughput promise for blockchain indexing.

The implementation lives in [research_pool](../../src/economic_machine/research_pool/).
[worker.py](worker.py) is the explicit operator entry point. The native adapters
consume schema-checked proposals and verify the corresponding original evidence.
Models cannot create verified graph edges, sign transactions or place orders.

The queue records attempts, responses, locally rejected responses, reported
costs and unresolved reservations separately. Interrupted or uncertain requests
retain their reservation and are not automatically replayed. Provider cooldowns,
policy hashes, deadlines and the shared spending journal remain authoritative.

## Historical operating scope

This source snapshot preserves the original fifty-worker policy: a fixed model,
a shared monthly ceiling and an absolute deadline of `1792351694298` milliseconds.
That deadline has expired. The published worker therefore refuses new work under
that historical policy. Publication does not renew its authority or resume an
operator's previous run. No active policy, credentials, private journals or
service installation is included.

The preferred allocation is thirty Solana, fifteen Arbitrum and five Robinhood
lanes, with idle capacity reassigned. A pool may be idle while native checks or
new evidence are pending. Repeated calls on unchanged evidence are not progress.

## Verify offline

From a source checkout on an authorized Linux host:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[research]'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_research_pool_core.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_pump_research_pool.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_evm_research_pool_adapter.py'
```

These tests use temporary evidence and simulated provider responses. They do not
establish live trading, complete wallet histories, independent consensus finality
or profitability. Contributor-provider data collection requires separate approval
before sending any real data.

After building the native targets, `python3 scripts/verify_research_source.py`
runs the broader public research suites. Two optional integration checks may be
skipped when private retained pages and collector-generated protobuf bindings
are absent. Historical fixture clocks advance elapsed time inside the original
scope; production deadlines and their explicit expiry tests remain unchanged.
