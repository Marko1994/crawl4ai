# Production Deploy (this fork)

How the `prod-deploy` branch reaches a production host. This is fork-specific and
deliberately separate from `README.md`, which documents the generic upstream
Docker Hub / local-build flows.

The production host never builds from source. GitHub Actions
(`.github/workflows/prod-image.yml`) builds every push to `prod-deploy` and
publishes it to GHCR; the host only pulls.

```
push to prod-deploy  →  Actions builds  →  ghcr.io/marko1994/crawl4ai:latest  →  host pulls
```

---

## Prerequisites on the host

### 1. The `kb-crawl` network must already exist

`docker-compose.yml` declares it `external: true`, which means **compose will not
create it**. If it is missing, every `docker compose up` fails immediately with:

```
network kb-crawl declared as external, but could not be found
```

The network is owned by the kb-worker stack. On a host where that stack has never
run — a fresh VPS, a rebuilt box, or a machine where you are only bringing up
crawl4ai — create it first:

```bash
docker network ls | grep kb-crawl        # check
docker network create kb-crawl           # create if missing
```

Do not work around this by removing the network or making it non-external.
crawl4ai publishes **no host port**, so `kb-crawl` is the only route kb-worker has
to reach it (`http://crawl4ai:11235`).

### 2. `.llm.env` must exist on the host

It is gitignored, so it is never carried by a `git pull` — each host owns its own
copy. Create it from `.llm.env.example`. It supplies `CRAWL4AI_API_TOKEN` and the
provider API keys.

**The container reads `.llm.env` only at start.** Editing the file changes nothing
until the container is recreated — and conversely, a redeploy *activates* any edit
made since the last restart. If `CRAWL4AI_API_TOKEN` changed, every client pinned
to the old value starts getting `401` the moment you deploy.

---

## Where the API listens

`docker-compose.yml` publishes 11235, and only the **bind address** varies per
host:

```yaml
ports:
  - "${CRAWL4AI_BIND:-127.0.0.1}:11235:11235"
```

The default is loopback. A fresh clone, and production, therefore expose the API
to the host itself and to nothing on the network — which is what a
TLS-terminating reverse proxy on the same box needs, and all it needs. Containers
on `kb-crawl` reach the service as `http://crawl4ai:11235` regardless of this
setting; the published port is only about access from outside Docker.

A host that must serve other machines directly sets the override in a gitignored
`.env` beside `docker-compose.yml`:

```bash
# .env - THIS host only. Never deployed.
CRAWL4AI_BIND=0.0.0.0
```

> `.env` is **not** `.llm.env`. Compose reads `.env` when substituting `${...}`
> in the compose file itself; `.llm.env` is the `env_file` and only sets
> variables *inside* the container. They are unrelated, and a variable in the
> wrong one silently does nothing.

Because the default is safe, a host that never sets `CRAWL4AI_BIND` cannot end up
accidentally exposed, and cannot end up with no published port either — the
failure mode where compose succeeds, the container reports healthy, and nothing
answers.

To reach the API from the host for debugging, no configuration is needed: the
default loopback bind already allows `curl http://localhost:11235/...`.

### What publishing costs you

The bearer token becomes the only thing in front of the API. With
`security.api_token` empty in `config.yml` (the token is supplied by env instead)
`POST /token` is disabled, so no `data`-scope JWT can be issued and **every caller
that can authenticate holds `admin`** — deep crawl and LLM extraction included.

If the port is reachable, treat `CRAWL4AI_API_TOKEN` as a production secret:
rotate it if it has ever been committed, and prefer terminating TLS at a reverse
proxy rather than serving `:11235` directly, so the token is not sent in the clear.

---

## Deploy

```bash
# 1. Wait for the Actions run for your commit to go green.
#    Pulling early silently gets you the PREVIOUS image, and it looks like your
#    change simply did not work.

# 2. Confirm the prerequisite network exists (see above).
docker network ls | grep kb-crawl

# 3. Pull and restart.
docker compose pull && docker compose up -d

# 4. Wait for health. start_period is 40s, then /health every 30s.
docker compose ps          # expect: healthy
docker compose logs -f --tail=50 crawl4ai
```

### Verify from inside the network

There is no published host port, so `curl localhost:11235` from the host will not
work — that is intended, not a fault. Test from within the container:

```bash
docker compose exec crawl4ai curl -s http://localhost:11235/health
```

A healthy `/health` only proves the container booted. To prove the deployed image
is actually the one you built, exercise something version-specific.

---

## Rollback

```bash
IMAGE=ghcr.io/marko1994/crawl4ai TAG=<previous-tag> docker compose up -d
```

`IMAGE` and `TAG` both override the compose default, so any image works —
including upstream, if you need to fall all the way back:

```bash
IMAGE=unclecode/crawl4ai TAG=latest docker compose up -d
```

Be aware that upstream does **not** contain this fork's changes: the untrusted
config trust boundary, admin-scope gating for deep crawl and LLM extraction, the
egress/SSRF broker, or the flat `/crawl` request fields. Falling back to upstream
is a security posture change, not just a version change.

---

## Gotchas that have actually bitten

- **Check which image is really running** before assuming a deploy is a small
  step. A host left on `unclecode/crawl4ai:latest` is running upstream code, so
  the first `docker compose up` swaps in this fork's entire divergence at once,
  not just your latest commit:
  ```bash
  docker inspect <container> --format '{{.Config.Image}}'
  ```
- **A running container can disagree with this repo.** It keeps the image, env,
  and port bindings it was created with. Ports are the common surprise: a
  container started with `-p 11235:11235` keeps publishing on `0.0.0.0` even
  though this compose file publishes nothing, and the next `up -d` closes it.
  Always check the live container rather than inferring from the compose file:
  ```bash
  docker inspect <container> --format '{{json .HostConfig.PortBindings}}'
  ```
- **Never set `CRAWL4AI_BIND=0.0.0.0` on a production host.** The default
  loopback bind is already enough for a reverse proxy on the same box, and for
  local `curl`. Binding all interfaces publishes an admin-scoped API to the
  network with the bearer token as the only control.
- **`POST /token` is disabled** when `config.yml` has an empty
  `security.api_token` (the token is supplied via the env var instead). No
  `data`-scope JWT can be issued, so **every caller that can authenticate holds
  `admin` scope**. Treat `CRAWL4AI_API_TOKEN` as a full-privilege credential.

---

## Related

- `API_DOCUMENTATION.md` — the API surface, auth scopes, and provider allowlist
- `ARCHITECTURE.md` — how the server fits together
- `SECURITY-VERIFY.md` — verifying the hardened posture
