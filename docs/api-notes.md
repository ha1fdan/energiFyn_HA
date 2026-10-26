# Energi Fyn app API (iOS app 3.4.9, captured with mitmproxy)

## Login: OIDC authorization code + PKCE, no MitID needed for email/password accounts
- `GET accounts.forsyningslogin.dk/connect/authorize` (`client_id=ef-app`,
  `redirect_uri=energifyn://authorize`, `scope=openid profile customer offline_access`,
  `prompt=login`, S256 challenge) → 302 to `/login`.
- `POST /login` form: `Username`, `Password`, `ReturnUrl`, `button`, `__RequestVerificationToken`.
  Success returns an HTML page with `<meta http-equiv="refresh" content="0;url=/connect/authorize/callback?...">`;
  wrong credentials return the form again (no meta refresh).
- That callback 302s to `energifyn://authorize?code=...&state=...`.
- `POST /connect/token` (`grant_type=authorization_code`, `client_id`, `client_secret` (public, in the app),
  `code_verifier`, `redirect_uri`, `code`) → `access_token` (1 h), `refresh_token`.
- `grant_type=refresh_token` works and **rotates the refresh token on every use**.
- `GET efprofileservice.azurewebsites.net/api/customers` → customer numbers.
- `GET efprofileservice.azurewebsites.net/api/customers/{cid}/o2-token` (Bearer access token)
  → a 30-day JWT. **This is the Bearer token for webapi.**

## webapi.energifyn.dk/api/v4 (headers: Bearer, `Client-Identity: APP_EF/3.4.9/production`, `X-App-Version`)
- `GET /customers/{cid}/estate` → estates with `meters[]`.
- Meter id in URLs is `{number}-{installationId}`.
- `POST /customers/{cid}/estates/{estateId}/meters/{meterId}/consumption/detail`
  `{startDate, endDate, aggregateFactor: Hourly|Daily, displayType: Daily|Monthly}`
  → `period{consumption, price, ...}`, `consumptionValues[]{date, meassurements[{kWh}], price, isEstimatedMeassurement}`.
  Dates are local Danish time without offset. Today → HTTP 500 "Consumption could not be found".
