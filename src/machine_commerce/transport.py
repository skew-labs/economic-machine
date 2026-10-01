"""Bounded, DNS-pinned HTTPS egress and independent EVM payment observation."""

import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx
from eth_utils import keccak

from economic_machine.values import MachineError

from .x402 import address


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise MachineError("bounded HTTPS URL required")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.query or parsed.port not in {None, 443}):
            raise MachineError("HTTPS on port 443 without query credentials required")
    except ValueError as exc:
        raise MachineError("invalid HTTPS URL") from exc
    return parsed


class PinnedTransport(httpx.BaseTransport):
    def __init__(self, urls, resolver=socket.getaddrinfo, delegate=None):
        self.urls = frozenset(urls)
        self.resolver = resolver
        self.delegate = delegate or httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request):
        original = str(request.url)
        if original not in self.urls:
            raise MachineError("outbound URL is not registered")
        parsed = https_url(original)
        resolved = self.resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = sorted({r[4][0] for r in resolved})
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise MachineError("private or mixed DNS destinations are forbidden")
        request.url = request.url.copy_with(host=ips[0])
        request.headers["Host"] = parsed.hostname
        request.extensions["sni_hostname"] = parsed.hostname
        return self.delegate.handle_request(request)

    def close(self):
        self.delegate.close()


class HTTPS:
    def __init__(self, urls):
        for url in urls:
            https_url(url)
        self.urls = frozenset(urls)

    def call(self, url, body, headers=None):
        if len(json.dumps(body).encode()) > 50_000:
            raise MachineError("outbound request exceeds size limit")
        with httpx.Client(transport=PinnedTransport(self.urls), trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(10, connect=3)) as client, \
                client.stream("POST", url, json=body, headers=headers or {}) as response:
            data, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 200_000:
                    raise MachineError("outbound response exceeds size limit")
                data.append(chunk)
            # Header values may contain authorizations; never log this return value.
            return response.status_code, dict(response.headers), b"".join(data)


def integer(value):
    if not isinstance(value, str) or not value.startswith("0x") or not 3 <= len(value) <= 66:
        raise MachineError("invalid chain integer")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise MachineError("invalid chain integer") from exc


def hash32(value):
    if not isinstance(value, str) or len(value) != 66:
        raise MachineError("chain hash required")
    integer(value)
    return value.lower()


def topic_address(value):
    return "0x" + address(value)[2:].rjust(64, "0")


TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
AUTH_USED = "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()
AUTH_STATE = "0x" + keccak(text="authorizationState(address,bytes32)")[:4].hex()


class Chain:
    def __init__(self, transport):
        self.transport = transport

    def rpc(self, url, method, params):
        try:
            status, _, content = self.transport.call(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except (httpx.HTTPError, OSError) as exc:
            raise MachineError("chain observation unavailable") from exc
        if status != 200:
            raise MachineError("chain observation unavailable")
        try:
            result = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise MachineError("invalid chain response") from exc
        if not isinstance(result, dict) or result.get("id") != 1 or "error" in result or "result" not in result:
            raise MachineError("chain observation unavailable")
        return result["result"]

    def observe(self, profile, authorization, tx_hash, start_block):
        url = profile["rpc_url"]
        if integer(self.rpc(url, "eth_chainId", [])) != int(profile["network"].split(":")[1]):
            raise MachineError("observation chain differs from approved network")
        head = self.rpc(url, "eth_getBlockByNumber", [profile["finality"], False])
        if not isinstance(head, dict):
            raise MachineError("finality observation unavailable")
        head_number, head_time = integer(head["number"]), integer(head["timestamp"])
        hash32(head["hash"])
        asset, payer, nonce = address(profile["asset"]), address(authorization["from"]), hash32(authorization["nonce"])
        if not tx_hash or head_number - start_block <= 10000:
            if head_number - start_block > 10000:
                raise MachineError("nonce search exceeds reconciliation window; explicit transaction evidence required")
            logs = self.rpc(url, "eth_getLogs", [{"address": asset, "fromBlock": hex(min(start_block, head_number)),
                "toBlock": hex(head_number), "topics": [AUTH_USED, topic_address(payer), nonce]}])
            if not isinstance(logs, list) or len(logs) > 1:
                raise MachineError("ambiguous authorization consumption")
            if logs:
                tx_hash = hash32(logs[0]["transactionHash"])
        if tx_hash:
            receipt = self.rpc(url, "eth_getTransactionReceipt", [hash32(tx_hash)])
            if not receipt:
                return {"status": "PENDING", "tx_hash": tx_hash}
            if (hash32(receipt["transactionHash"]) != hash32(tx_hash)
                    or integer(receipt["blockNumber"]) > head_number):
                return {"status": "PENDING", "tx_hash": tx_hash}
            block = self.rpc(url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
            if hash32(block["hash"]) != hash32(receipt["blockHash"]):
                return {"status": "PENDING", "tx_hash": tx_hash, "reason": "NONCANONICAL_RECEIPT"}
            if integer(receipt["status"]) == 1:
                transfer, used = [], []
                for log in receipt["logs"]:
                    if (log.get("removed") or address(log["address"]) != asset
                            or hash32(log["transactionHash"]) != hash32(tx_hash)
                            or hash32(log["blockHash"]) != hash32(receipt["blockHash"])):
                        continue
                    topics = [t.lower() for t in log["topics"]]
                    if topics == [AUTH_USED, topic_address(payer), nonce]:
                        used.append(log)
                    if topics == [TRANSFER, topic_address(payer), topic_address(authorization["to"])]:
                        transfer.append(integer(log["data"]))
                if len(used) == 1 and transfer == [int(authorization["value"])]:
                    return {"status": "PAID", "tx_hash": tx_hash, "block_hash": receipt["blockHash"],
                        "block_number": integer(receipt["blockNumber"]), "finality": profile["finality"],
                        "asset": asset, "payer": payer, "pay_to": address(authorization["to"]),
                        "amount_atoms": authorization["value"], "nonce": nonce}
                # A mismatched reported receipt is not permission to retry a signature.
                return {"status": "PENDING", "tx_hash": tx_hash, "reason": "PAYMENT_EVIDENCE_MISMATCH"}
        if head_time >= int(authorization["validBefore"]):
            consumed = integer(self.rpc(url, "eth_call", [{"to": asset, "data":
                AUTH_STATE + payer[2:].rjust(64, "0") + nonce[2:]}, hex(head_number)]))
            if consumed == 0:
                return {"status": "EXPIRED_UNPAID", "tx_hash": tx_hash, "finalized_at": head_time,
                        "finality": profile["finality"], "nonce": nonce}
        return {"status": "PENDING", "tx_hash": tx_hash}
