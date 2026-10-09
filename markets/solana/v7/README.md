# Solana v7: undeployed optimization candidate

v7 retains the fixed wide arena and nonce/session/risk/conservation checks, while
rounding one fee over cumulative IOC notional and caching funding/risk state across
contiguous maker fills. The fee correction is a behavioral change. CU reductions
are compared against a fee-corrected baseline, not the older cheaper semantics.

The `production` feature uses `MPERPS03`, pins a receiver at initialization and
rejects administrator price/funding updates and later mode switches. It supports
plain, freeze-authority-free six-decimal SPL collateral. It is neither the deployed
v6 binary nor a claim of USDC/Token-2022 support.

```sh
cd markets/solana/v7
bash build-native.sh
bash build-sbf.sh wide256
export MP_LAYOUT=wide256
export MP_ELF="$PWD/build/sbf/machine_perps.so"
export PYTHONPATH="$PWD/tests:$PWD/agents:$PWD/client:$PWD/muse"
python ../../run-solana-tests.py --version v7
```

`test_production_init.py` requires the production ELF and must run separately:

```sh
bash build-sbf.sh production
MP_ELF="$PWD/build/sbf/machine_perps.so" \
  python ../../run-solana-tests.py --version v7 --mode production
```

Exclude that production-only module from a wide-layout suite. The public helper
keeps the artifacts under `build/sbf`; preserve one output before switching modes.
Recorded candidate SHA-256 values are in the source provenance file. They are
historical build identities, not newly deployed programs.

The receiver in offline tests is a fixture. Test success does not prove a real
price-feed deployment, keeper availability, public congestion performance or
capitalized backstop operation. Research/evaluation shared with v6 is published
in the v6 directory rather than copied into this candidate.
