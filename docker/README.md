# LAMA — Docker deployment

Single-image bundle: **React frontend + FastAPI backend + MongoDB 7 + Nginx**, all wrapped under `supervisord`. The only thing it does **not** bundle is Qdrant — pass `QDRANT_URL` + `QDRANT_API_KEY` from your existing instance at runtime.

| | Value |
|---|---|
| Image | `mishramesh/lama:latest` |
| Public port | **8382** (HTTP) |
| Persistent volume | `/data/db` (MongoDB) |
| Architecture | linux/amd64 |

---

## 1. Build & push (CI or your workstation)

```bash
# from repo root
docker login                              # use your Docker Hub creds (push rights on mishramesh/lama)
./docker/build-and-push.sh                # builds + pushes :latest
./docker/build-and-push.sh v1.0.0         # tag a release
```

The build is fully self-contained — no Emergent footprint, no external scripts injected into HTML.

---

## 2. Deploy on Hostinger (Docker Manager — compose) — recommended

In **Hostinger VPS → Docker Manager → Stacks**, create a new stack and paste the contents of `/docker-compose.yml`. Add the env-vars from `.env.example` (Hostinger lets you paste a `.env` block or fill key-value pairs). Click **Deploy**.

It will:
- Pull `mishramesh/lama:latest` from Docker Hub
- Expose `:8382` on the VPS
- Persist MongoDB to the named volume `lama_mongo_data`
- Restart automatically on reboot
- Run a `/health` check every 30s

Upgrade to a new release later by clicking **Pull & redeploy** in Docker Manager (the `pull_policy: always` line in compose makes this one-click).

### Or, plain `docker compose` on any Linux box

```bash
cd /opt/lama
curl -fsSL https://raw.githubusercontent.com/mishramesh/lama/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/mishramesh/lama/main/.env.example  -o .env
$EDITOR .env       # fill in OPENROUTER_API_KEY + QDRANT_URL + QDRANT_API_KEY
docker compose pull
docker compose up -d
docker compose logs -f lama
```

---

## 3. Deploy on Hostinger (plain `docker run`) — alternative

```bash
ssh root@<vps-ip>

# Install Docker once
curl -fsSL https://get.docker.com | sh

# Create a persistent volume for MongoDB
docker volume create lama_mongo_data

# Pull
docker pull mishramesh/lama:latest

# Run (replace QDRANT_URL / API key with your live values)
docker run -d \
  --name lama \
  --restart unless-stopped \
  -p 8382:8382 \
  -v lama_mongo_data:/data/db \
  -e OPENROUTER_API_KEY="sk-or-v1-..." \
  -e QDRANT_URL="http://93.127.194.188:6333" \
  -e QDRANT_API_KEY="your_secret_api_key_here" \
  -e DB_NAME="lama" \
  mishramesh/lama:latest
```

Open `http://<vps-ip>:8382/` in your browser.

### Health probe
```bash
curl http://<vps-ip>:8382/health   # → {"ok": true}
```

### Logs
```bash
docker logs -f lama                # entrypoint + supervisord summary
docker exec -it lama tail -f /var/log/backend.err.log
docker exec -it lama tail -f /var/log/mongodb.log
```

### Upgrade in place
```bash
docker pull mishramesh/lama:latest
docker stop lama && docker rm lama
# re-run the same docker run command above
# MongoDB data survives because of the named volume.
```

### Stop / remove
```bash
docker stop lama && docker rm lama
# Keeps lama_mongo_data volume. To also wipe data:
docker volume rm lama_mongo_data
```

---

## Environment variables (all optional — defaults shown)

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | _(empty)_ | Required for any LLM call until you configure a provider via the Console UI. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Override if using a proxy. |
| `LAMA_DEFAULT_MODEL` | `deepseek/deepseek-chat` | Fallback model when no provider is set in Console. |
| `QDRANT_URL` | _(empty)_ | Required for KB-RAG chat. |
| `QDRANT_API_KEY` | _(empty)_ | If your Qdrant requires auth. |
| `DB_NAME` | `lama` | MongoDB database name. |
| `MONGO_URL` | `mongodb://127.0.0.1:27017` | Don't override unless you want to point at an external Mongo. |
| `MONGO_DATA_DIR` | `/data/db` | Don't override — Dockerfile volume targets `/data/db`. |

---

## Image internals

```
/app/backend          FastAPI source
/usr/share/nginx/html React production build (served by Nginx)
/data/db              MongoDB data (VOLUME)
/etc/nginx/nginx.conf Reverse-proxy + SPA fallback
/etc/supervisor/      supervisord.conf (mongod + uvicorn + nginx)
/entrypoint.sh        writes /app/backend/.env from runtime env then `exec supervisord`
```

Processes:
- `mongod` on `127.0.0.1:27017`
- `uvicorn server:app` on `0.0.0.0:8001`
- `nginx` on `0.0.0.0:8382` — proxies `/api/*` to `:8001`, serves React for everything else

---

## Troubleshooting

- **Container starts but `/health` returns 502** — MongoDB still warming up; wait ~20s.
- **`/api/chat` returns 401/500** — `OPENROUTER_API_KEY` is missing or invalid.
- **KB chat fails** — `QDRANT_URL` not reachable from inside the container. From the host: `docker exec lama curl -fsS $QDRANT_URL/collections`.
- **Disk full** — MongoDB volume can grow with large KB ingestion. Inspect: `docker exec lama du -sh /data/db`.
