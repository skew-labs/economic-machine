"""Operator-owned RPC endpoints; endpoint credentials never enter receipts."""
import os
from urllib.parse import urlsplit

from economic_machine.values import MachineError


def configured_rpcs(env=None):
    env = os.environ if env is None else env
    endpoints = (
        env.get("SKEW_FUEL_RPC_PRIMARY", "https://arb1.arbitrum.io/rpc"),
        env.get("SKEW_FUEL_RPC_VERIFIER", "https://arbitrum-one-rpc.publicnode.com"),
    )
    hosts = []
    for endpoint in endpoints:
        try:
            parts = urlsplit(endpoint)
            valid = (parts.scheme == "https" and parts.hostname and not parts.username
                     and not parts.password and not parts.fragment and parts.port in (None, 443)
                     and not any(c.isspace() for c in endpoint))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise MachineError("INVALID_FUEL_RPC_CONFIGURATION")
        hosts.append(parts.hostname.lower())
    if hosts[0] == hosts[1]:
        raise MachineError("INDEPENDENT_FUEL_RPC_HOSTS_REQUIRED")
    return endpoints


def source_label(index):
    return ("ARBITRUM_PRIMARY", "ARBITRUM_VERIFIER")[index]
