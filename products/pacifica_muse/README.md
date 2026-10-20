# Muse market-making adapter

Muse proposes bounded policies and research graphs. Economic Machine validates
them before a native decision kernel can use them. The model stays outside the
quote, signing and account-reconciliation path.

The reusable implementation includes:

- [Policy and context validation](../../src/economic_machine/muse_maker.py).
- [Accounted policy worker](../../src/economic_machine/muse_policy_worker.py).
- [Receipt accounting](../../src/economic_machine/muse_receipts.py).
- [Research graph validation](../../src/economic_machine/muse_learning.py).
- [Pure native maker kernel](../../native/include/machine/economics/muse_maker.hpp).
- [Native kernel tests](../../native/tests/muse_maker.cpp).

The pinned model is `meta/muse-spark-1.3-contributor`. Model outputs are proposals;
they cannot extend a policy lifetime, increase a session's collateral or exposure,
release an uncertain reservation, or substitute for attributed exchange receipts.
Training on proprietary trading-firm strategies and profitable operation are not
claims made by this package.

The venue-specific [native adapter](native/) and build scripts are retained as
integration source. They require an independently supplied venue parser,
transport and startup headers in `venue-fixture`, plus libcurl and OpenSSL. Those
private deployment dependencies, credentials and release manifests are not
bundled. This adapter is not a standalone live-trading release.

The original operator launcher retains fixed session and exposure checks. No
launcher is installed or executed by importing the package. An operator must
separately supply an authorized credential, matching build manifest and session.

Build the reusable kernels on an authorized Linux host:

```sh
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
PYTHONPATH=src MUSE_RESEARCH_LIB="$PWD/build/native/libmachine_muse_learning.so" \
  python3 -m unittest discover -s tests -p 'test_muse*.py'
```

These checks use fixtures. An order acknowledgment is not a fill; a fill is not
complete account settlement. Missing data, uncertain requests and fee attribution
remain explicit in the receipt and recovery code.
