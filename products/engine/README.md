<img src="../../site/assets/app-engine.svg" width="64" alt="Engine">

# Engine

Named agents use common connections and a shared budget. Approval binds to an exact plan;
ambiguous execution keeps its reservation.

[Install](../../docs/SELF_HOSTING.md) · [Kernel architecture](../../docs/ENGINE_ARCHITECTURE.md)

| Area | Source |
| --- | --- |
| Typed state, ISA, compiler, invariants, transitions, receipts | [economic_machine](../../src/economic_machine/) |
| Connections, policies, scheduler, adapters, native bindings | [machine_engine](../../src/machine_engine/) |
| C++ arithmetic, bounded programs and state runtime | [native](../../native/) |
| Owner console | [web](../../web/) |

Use `economic-machine spec` to read the ISA and `economic-machine compile --program <file>`
to validate before registration. Evaluate the synthetic example at its historical time:

```sh
.venv/bin/economic-machine run --program cases/economic_program_demo.json \
  --state cases/economic_state_demo.json --at 2026-09-25T12:01:00+00:00
```

This emits a candidate and an authorization state, not a live trade. Only a configured adapter
can submit an approved operation. Venue trading and external service purchases use separate
accounting units and settlement paths. LLM inference is outside the monitoring loop.
