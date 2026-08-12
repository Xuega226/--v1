# Qdrant

Qdrant runs as a reusable local Docker service. Its ports are bound to
`127.0.0.1`, so it is not exposed to the LAN.

The image is pinned to Qdrant `v1.18.3` to avoid unplanned upgrades.

## Start

```powershell
docker compose -f infra/qdrant/compose.yaml up -d
```

## Check

```powershell
docker compose -f infra/qdrant/compose.yaml ps
Invoke-RestMethod http://127.0.0.1:6333/healthz
```

Dashboard: <http://127.0.0.1:6333/dashboard>

## Stop

```powershell
docker compose -f infra/qdrant/compose.yaml down
```

The named volume `unnameko_qdrant_data` keeps the database when the container
is recreated. Do not add `--volumes` to the stop command unless the stored
vectors should be deleted intentionally.
