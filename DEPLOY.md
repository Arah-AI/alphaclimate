# Deploying AlphaClimate

Target: `alphaclimate.withsummon.com` on the Summon VPS (`72.60.78.94`), Dokploy
at `:3000`, same pattern as `lab-ocr` and `mmf-core`.

Run these yourself: the keychain reads are blocked in the agent session, and the
`summon` host is password-auth only.

---

## 1. DNS first, always

The vault note on this is emphatic and it is right: create the DNS record
**before** adding the domain in Dokploy. If Dokploy registers a domain that does
not yet resolve, the ACME order fails, Traefik caches the backoff, and the host
serves `TRAEFIK DEFAULT CERT` for about ten minutes no matter how many times you
redeploy. Recovering means deleting and recreating the domain in Dokploy.

```bash
TOKEN=$(security find-generic-password -s cloudflare-dns-token -a summon -w)
ZONE=066865256d8c807847cee32a7a168395

curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/dns_records" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data '{"type":"A","name":"alphaclimate.withsummon.com","content":"72.60.78.94","ttl":1,"proxied":false}' \
  | python3 -m json.tool
```

`proxied:false` matters: Let's Encrypt has to reach the origin directly for the
first issuance. Turn the orange cloud on afterwards if you want it.

Confirm it resolves before moving on:

```bash
dig +short alphaclimate.withsummon.com    # must print 72.60.78.94
```

---

## 2. Create the app in Dokploy

The repo is `RafieAmandio/alphaclimate`, private, branch `main`. It ships a
`docker-compose.yml` with two services (`web` on 3000, `api` on 8000), so create
it as a **Compose** application, not a Dockerfile application.

In the Dokploy UI:

1. **Create → Compose**, name `alphaclimate`.
2. **Provider:** GitHub → `RafieAmandio/alphaclimate`, branch `main`.
   Dokploy needs access to a private repo, so either install the Dokploy GitHub
   app on it or switch the repo to public.
3. **Compose path:** `docker-compose.yml`.
4. **Domains → Add domain:**
   - Host: `alphaclimate.withsummon.com`
   - Service: `web`
   - Container port: `3000`
   - HTTPS on, certificate provider Let's Encrypt
5. **Deploy.**

No environment variables are required. `API_URL` is already set to
`http://api:8000` in the compose file, and both the curve file and the warmed
hazard cache are baked into the API image.

### If you prefer the API

```bash
DOK=$(security find-generic-password -s <dokploy-service> -a summon -w)
BASE=http://72.60.78.94:3000/api

curl -s -X POST "$BASE/compose.deploy" \
  -H "x-api-key: $DOK" -H "Content-Type: application/json" \
  --data '{"composeId":"<id-from-the-ui>"}'
```

Grab `composeId` from the app URL in the Dokploy UI after step 1.

---

## 3. Verify

```bash
curl -s https://alphaclimate.withsummon.com/api/health | python3 -m json.tool
```

Expected:

```json
{
  "status": "ok",
  "hazard_source": "os-climate-physical-risk/hazard-indicators/hazard.zarr",
  "hazard_points_cached": 900,
  "curves_loaded": 265,
  "curve_gaps": 9,
  "assets": 12
}
```

`"status": "degraded"` means the API cannot read `data/hazard_cache.json`. Check
that the file was committed and that the `COPY` in `api/Dockerfile` found it.

Then open `https://alphaclimate.withsummon.com` and confirm the dashboard renders
with non-zero numbers and no degraded banner.

---

## 4. If the certificate does not issue

Straight from the vault note, because this has bitten before:

1. Delete the domain in Dokploy.
2. Recreate it.
3. Deploy again.

That forces a fresh Traefik router and a fresh ACME order, and the certificate
lands in under a minute. Restarting Traefik also works but disrupts every other
site on the box, so try this first.

---

## Refreshing the hazard data

The cache is a build artefact, not a runtime dependency. To rebuild it after
changing the portfolio or the array catalogue:

```bash
source .venv/bin/activate
python scripts/warm_cache.py     # ~15 min, pulls from the public S3 store
git add data/hazard_cache.json && git commit -m "Refresh hazard cache" && git push
```

Then redeploy. The API never calls S3 on a web request, so a warm run is the
only time the store is touched.
