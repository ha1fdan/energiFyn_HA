"""Smoke test: login, refresh, list meters, fetch last month's daily kWh.

Usage: EF_USER=... EF_PASS=... python3 scripts/try_login.py
"""

import asyncio
import importlib.util
import os
from datetime import datetime, timedelta

import aiohttp

# Load api.py directly so the HA package __init__ (needs homeassistant) isn't imported.
spec = importlib.util.spec_from_file_location(
    "ef_api",
    os.path.join(
        os.path.dirname(__file__), "..", "custom_components", "energi_fyn", "api.py"
    ),
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        c = api.EnergiFynClient(s)
        await c.login(os.environ["EF_USER"], os.environ["EF_PASS"])
        print("login ok, refresh token len", len(c.refresh_token))
        # Fresh client using only the refresh token, as HA will after a restart.
        c2 = api.EnergiFynClient(s)
        c2.refresh_token = c.refresh_token
        c2._access_token = c._access_token
        cust = (await c2.customers())[0]["customerNumber"]
        print("customer", cust)
        estates = await c2.estates(cust)
        for e in estates:
            for m in e["meters"]:
                print(
                    "estate",
                    e["estateId"],
                    "meter",
                    m["number"],
                    "active",
                    m["isActive"],
                )
        e, m = estates[0], estates[0]["meters"][0]
        end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        d = await c2.consumption(
            cust,
            e["estateId"],
            api.EnergiFynClient.meter_id(m),
            start,
            end,
            hourly=False,
        )
        for v in d["consumptionValues"]:
            print(
                v["date"][:10],
                v["meassurements"][0]["kWh"],
                "kWh",
                round(v["price"], 2),
                "kr",
            )
        print("refresh token rotated:", c2.refresh_token != c.refresh_token)


asyncio.run(main())
