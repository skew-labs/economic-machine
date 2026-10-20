"""One bounded HTTPS connection per reader; the shared RPC gate owns pacing."""
import http.client
from urllib.parse import urlsplit

from economic_machine.arbitrum_research.source import SourceBackoff, SourceHTTPError, SourceLimit, retry_after
from economic_machine.arbitrum_research.transport import read_body


class PersistentRPC:
    def __init__(self, url, *, factory=http.client.HTTPSConnection):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('RPC_HTTPS_REQUIRED')
        self.host, self.port = parsed.hostname, parsed.port or 443
        self.path = parsed.path or '/'
        if parsed.query:
            self.path += '?' + parsed.query
        self.factory = factory
        self.connection = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def post(self, body, limit):
        if self.connection is None:
            self.connection = self.factory(self.host, self.port, timeout=30)
        try:
            self.connection.request('POST', self.path, body=body, headers={
                'Content-Type': 'application/json', 'Accept-Encoding': 'gzip',
                'User-Agent': 'EconomicMachineResearch/0.1', 'Connection': 'keep-alive'})
            response = self.connection.getresponse()
            if response.status != 200:
                status = response.status
                delay = retry_after(response.headers.get('Retry-After'))
                self.close()
                if status in (408, 429, 500, 502, 503, 504, 529):
                    raise SourceBackoff(delay, status)
                raise SourceHTTPError(status)
            raw, truncated, metrics = read_body(response, limit)
            if truncated:
                raise SourceLimit('RPC_RESPONSE_BOUND')
            if response.will_close:
                self.close()
            return raw, metrics
        except (OSError, http.client.HTTPException) as error:
            self.close()
            raise SourceBackoff(60.0) from error
        except BaseException:
            # No hidden retry: each real attempt must pass the durable gate.
            self.close()
            raise
