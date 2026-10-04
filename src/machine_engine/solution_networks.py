"""Chainlink official network configuration; independent readback is still mandatory."""

NETWORKS = {
    42161: {
        "name": "Arbitrum One",
        "coordinator": "0x3C0Ca683b403E37668AE3DC4FB62F4B29B6f7a3e",
        "link": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
        "key_hash": "0x9e9e46732b32662b9adc6f3abdf6c5e926a666d174a4d6b8e39c4cca76a38897",
    },
    421614: {
        "name": "Arbitrum Sepolia",
        "coordinator": "0x5CE8D5A2BC84beb22a398CCA51996F7930313D61",
        "link": "0xb1D4538B4571d411F07960EF2838Ce337FE1E80E",
        "key_hash": "0x1770bdc7eec7771f7ba4ffd640f34260d7f095b79c92d34a5b2551d6f6cfd2be",
    },
}
SOURCE = "https://docs.chain.link/vrf/v2-5/supported-networks"
SECURITY = "https://docs.chain.link/vrf/v2-5/security"
