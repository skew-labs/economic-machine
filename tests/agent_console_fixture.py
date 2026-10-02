"""Disposable loopback browser fixture. No vendor network or real money.

Run only on the remote verification host; tunnel its loopback port to 18803.
This known test token has authority only over a temporary fixture database.
"""

import os
import tempfile
import time
from pathlib import Path

import uvicorn
from test_engine_execution import SPOT, VenueFixture

from machine_engine.api import create_engine_app
from machine_engine.broker import BinanceBroker
from machine_engine.connections import Connectors
from machine_engine.workspace import Workspace


def main():
    if not str(Path(__file__).resolve()).startswith("/srv/skew/"):
        raise SystemExit("Remote verification only")
    os.environ.update({"VENUE_KEY": "fixture-exchange-access", "VENUE_SECRET": "fixture-exchange-secret"})
    http = VenueFixture()
    with tempfile.TemporaryDirectory(prefix="machine-agent-ui-") as directory:
        work = Workspace(
            Path(directory) / "fixture.sqlite3",
            readers=Connectors(http),
            broker_factory=lambda connection, clock: BinanceBroker(connection, http=http, clock=clock),
            live_enabled=False,
        )
        cid = work.connect(SPOT | {"name": "Disposable fixture · Binance Spot"})["id"]
        work.sync(cid)
        for name in ["FIXTURE_ALPHA_RULES", "FIXTURE_VAULT_RULES"]:
            work.trading.policy(
                {
                    "name": name,
                    "connection_id": cid,
                    "symbols": ["BTCUSDT"],
                    "sides": ["BUY", "SELL"],
                    "turnover_limit_usdt": "20",
                    "max_order_usdt": "10",
                    "fee_reserve_bps": "20",
                    "expires_at": int(time.time()) + 3600,
                }
            )
        app = create_engine_app(
            work.runtime.db_path,
            workspace=work,
            admin_token="disposable-agent-console-fixture-owner",
            origin="http://127.0.0.1:18803",
        )

        # Explicitly label the fixture in every console page; never simulate a user's account.
        @app.middleware("http")
        async def label_fixture(request, call_next):
            response = await call_next(request)
            if request.url.path in {"/", "/console", "/engine"}:
                from fastapi.responses import HTMLResponse

                chunks = [part async for part in response.body_iterator]
                source = (
                    b"".join(chunks)
                    .decode()
                    .replace("Personal workspace", "Disposable UI fixture")
                    .replace("Economic runtime", "Isolated test adapters")
                )
                headers = dict(response.headers)
                headers.pop("content-length", None)
                return HTMLResponse(source, headers=headers)
            return response

        uvicorn.run(app, host="127.0.0.1", port=8803, access_log=False)


if __name__ == "__main__":
    main()
