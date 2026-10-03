"""Server-only PayPal Orders v2 sandbox adapter; no live-money endpoint."""

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from economic_machine.values import MachineError, require_keys
from machine_commerce.transport import PinnedTransport

BASE = "https://api-m.sandbox.paypal.com"
ID = re.compile(r"[A-Z0-9]{8,32}\Z")


def provider_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise MachineError("PAYPAL_INVALID_PROVIDER_ID")
    return value


class PayPalSandbox:
    """Credentials are environment references. Response bodies never reach the console."""

    def __init__(self, config):
        require_keys(
            config,
            {"merchant_id", "client_id_env", "client_secret_env", "public_origin", "services"},
            "PayPal sandbox config",
        )
        self.merchant = provider_id(config["merchant_id"])
        for key in ("client_id_env", "client_secret_env"):
            if not isinstance(config[key], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,120}", config[key]):
                raise MachineError("PAYPAL_CREDENTIAL_REFERENCE_REQUIRED")
        origin = urlsplit(config["public_origin"])
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.port not in (None, 443)
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path
        ):
            raise MachineError("PAYPAL_PUBLIC_HTTPS_ORIGIN_REQUIRED")
        services = config["services"]
        if not isinstance(services, list) or not 1 <= len(services) <= 16:
            raise MachineError("PAYPAL_ADMITTED_SERVICES_REQUIRED")
        self.services = {}
        for service in services:
            require_keys(
                service, {"sku", "version", "kind", "worker", "price_cents", "name"}, "admitted USD service"
            )
            if (
                service["worker"] != "csv_cleanup_v1"
                or service["kind"] != "data_cleanup"
                or not isinstance(service["sku"], str)
                or not re.fullmatch(r"[a-z0-9-]{1,64}", service["sku"])
                or type(service["version"]) is not int
                or service["version"] < 1
                or type(service["price_cents"]) is not int
                or not 1 <= service["price_cents"] <= 1000000
                or not isinstance(service["name"], str)
                or not 1 <= len(service["name"]) <= 100
                or service["sku"] in self.services
            ):
                raise MachineError("PAYPAL_INVALID_ADMITTED_SERVICE")
            self.services[service["sku"]] = dict(service)
        self.config = dict(config)

    @classmethod
    def configured(cls):
        name = os.environ.get("ENGINE_PAYPAL_SANDBOX_FILE")
        if not name:
            return None
        path = Path(name)
        if not path.is_absolute() or path.is_symlink():
            raise MachineError("PAYPAL_PRIVATE_CONFIG_REQUIRED")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 50000:
                raise MachineError("PAYPAL_PRIVATE_CONFIG_REQUIRED")
            return cls(json.loads(stream.read(50001)))

    def credentials_available(self):
        return bool(
            os.environ.get(self.config["client_id_env"]) and os.environ.get(self.config["client_secret_env"])
        )

    def _request(self, method, path, *, body=None, request_id=None, oauth=False):
        url = BASE + path
        headers = {"Accept": "application/json", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        args = {}
        if oauth:
            client = os.environ.get(self.config["client_id_env"])
            secret = os.environ.get(self.config["client_secret_env"])
            if not client or not secret:
                raise MachineError("PAYPAL_SANDBOX_CREDENTIALS_UNAVAILABLE")
            args = {"auth": (client, secret), "data": {"grant_type": "client_credentials"}}
        else:
            token = self._request("POST", "/v1/oauth2/token", oauth=True).get("access_token")
            if not isinstance(token, str) or not 10 <= len(token) <= 4096 or any(ord(c) < 33 for c in token):
                raise MachineError("PAYPAL_INVALID_OAUTH_RESPONSE")
            headers["Authorization"] = "Bearer " + token
            if body is not None:
                args["json"] = body
        try:
            with (
                httpx.Client(
                    transport=PinnedTransport([url]),
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(12, connect=3),
                ) as client,
                client.stream(method, url, headers=headers, **args) as response,
            ):
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 200000:
                        raise MachineError("PAYPAL_RESPONSE_LIMIT")
                if not 200 <= response.status_code < 300:
                    raise MachineError("PAYPAL_REQUEST_NOT_CONFIRMED")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise MachineError("PAYPAL_INVALID_RESPONSE")
                return result
        except Exception as exc:
            # Never leak auth headers, customer email, provider details or URLs.
            raise MachineError("PAYPAL_REQUEST_NOT_CONFIRMED") from exc

    def create(self, plan, request_id):
        return self._request(
            "POST",
            "/v2/checkout/orders",
            request_id=request_id,
            body={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "reference_id": plan["id"],
                        "custom_id": plan["hash"],
                        "payee": {"merchant_id": self.merchant},
                        "amount": {"currency_code": "USD", "value": plan["amount"]},
                    }
                ],
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "user_action": "PAY_NOW",
                            "shipping_preference": "NO_SHIPPING",
                            "return_url": self.config["public_origin"] + "/commerce/console#tasks",
                            "cancel_url": self.config["public_origin"] + "/commerce/console#tasks",
                        }
                    }
                },
            },
        )

    def read(self, oid):
        return self._request("GET", "/v2/checkout/orders/" + provider_id(oid))

    def capture(self, oid, request_id):
        return self._request(
            "POST", "/v2/checkout/orders/" + provider_id(oid) + "/capture", request_id=request_id, body={}
        )


def bound_order(order, plan, merchant, oid=None, *, paid=False):
    """Require one exact, merchant-bound, full USD capture from a fresh GET."""
    try:
        actual = provider_id(order["id"])
        if oid is not None and actual != oid:
            raise ValueError()
        (unit,) = order["purchase_units"]
        expected = {"currency_code": "USD", "value": plan["amount"]}
        if (
            order["intent"] != "CAPTURE"
            or unit["reference_id"] != plan["id"]
            or unit["custom_id"] != plan["hash"]
            or unit["payee"]["merchant_id"] != merchant
            or unit["amount"] != expected
        ):
            raise ValueError()
        if not paid:
            if order["status"] not in {"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED", "COMPLETED"}:
                raise ValueError()
            return actual
        (capture,) = unit["payments"]["captures"]
        if (
            order["status"] != "COMPLETED"
            or capture["status"] != "COMPLETED"
            or capture["amount"] != expected
            or capture["final_capture"] is not True
            or unit["payments"].get("refunds")
            or unit["payments"].get("authorizations")
        ):
            raise ValueError()
        return provider_id(capture["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MachineError("PAYPAL_ORDER_BINDING_FAILED") from exc
