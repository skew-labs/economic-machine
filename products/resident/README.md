# Resident agent workers

A parent model delegates bounded work to durable logical agents. Each agent has
its own mailbox, venue scope, quota, task journal and result cursor. A shared
small-model queue serves inference; a hundred identities do not mean a hundred
concurrent model processes or isolated hostile tenants.

The [library](../../src/economic_machine/resident/) implements grant checks,
idempotency, scheduling and validated local-tool results. Model suggestions stay
proposals for the parent. Native signal gates use integer inputs, monotonic
source sequence numbers and explicit expiry.

[provision.py](provision.py) and [parent.py](parent.py) require explicit operator
configuration. They do not attach exchange accounts or trading credentials.
[build_native.sh](build_native.sh) builds the native gate on an authorized Linux
host. Deployment manifests, local models, credentials and historical evidence
are excluded from this source snapshot.
