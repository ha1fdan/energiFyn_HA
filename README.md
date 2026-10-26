# Energi Fyn for Home Assistant

Unofficial custom integration that reads your electricity consumption from Energi Fyn,
the same data the Energi Fyn app shows. Not affiliated with or endorsed by Energi Fyn.

## Install (HACS)

1. HACS → ⋮ → Custom repositories → add this repo as type **Integration**.
2. Install **Energi Fyn**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **Energi Fyn**, and log in with the
   email and password from the app. The password is not stored, only a refresh token.

## Energy tab

The integration imports **hourly long-term statistics** with the real hour each kWh was used,
backfilled 30 days on first run:

| Statistic | Unit |
|---|---|
| `energi_fyn:<meter>_consumption` | kWh |
| `energi_fyn:<meter>_cost` | DKK (incl. VAT, as Energi Fyn bills it) |

Settings → Dashboards → Energy → **Electricity grid** → *Add consumption*:
1. Choose the statistic **"Elmåler consumption"** (not a sensor).
2. Under cost, pick **"Use an entity tracking the total costs"** and choose **"Elmåler cost"**.

These are statistics, not entities, on purpose. The data arrives about a day late, so entity
states would be recorded at the wrong time. Statistics are stamped with the real hour.

## Sensors (per meter)

Latest published day: consumption, cost, average price (DKK/kWh), peak hour (kWh, with the hour)
· last 7 days: consumption, cost · this month and last month: consumption, cost, benchmark
(kWh for similar homes) and % above/below benchmark · last meter reading, estimated annual
consumption, current electricity product and its price.

Disabled by default (enable them if you want them): home area, residents, heating, electric cars.

Not imported on purpose: the account address, contacts (contain CPR numbers) and broadband status.

## Limitations

- Energi Fyn publishes data about a day late. Nothing here is live, and today is never shown.
- Hourly data is imported as history, so the Energy dashboard fills in after each poll
  (every 3 h), not in real time.
- On the autumn DST change the API omits one hour; that hour is simply absent.
- The API is private and may change without notice.

## Development

```
python -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest
EF_USER=... EF_PASS=... python3 scripts/try_login.py   # live smoke test
```

## License

Code is MIT licensed, see [LICENSE](LICENSE). "Energi Fyn" and its logo
(`custom_components/energi_fyn/brand/icon.png`) belong to Energi Fyn and are not covered by that
license; they are used only to identify the service this integration talks to.
