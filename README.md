# LakeClaw

Most interactions within Databricks are ephemeral. You open a notebook, ask Genie a question, and get an answer, but the "intelligence" is scattered. It feels like hiring a brilliant consultant who forgets everything the moment they leave the room. Even with powerful built-in assistants, there is a fundamental lack of a persistent agent—one that is "always on," evolves as your data grows, and possesses the architectural "soul" that a project like OpenClaw provides. 

LakeClaw was built to fill this gap. By porting the [OpenClaw](https://github.com/openclaw) framework to Databricks Apps, we've created an agent that doesn't just run on the lake; it lives there. It's a freshwater crustacean designed to thrive in the Databricks ecosystem, using the Lakehouse as its long-term memory and its primary sensory input.

## Architecture

```
Telegram ──> Databricks App (LakeClaw Gateway)
                 │
                 ├── Databricks AI Gateway (OpenAI-compatible API; default: GPT 5.4)
                 │      (workspace URL uses ${DATABRICKS_WORKSPACE_ID}; PAT in DATABRICKS_TOKEN)
                 │
                 └── Unity Catalog Volume (soul, memories, sessions via MY_UC_VOLUME_PATH)
```

The app runs the OpenClaw gateway (`npm start` → `openclaw gateway run`) inside a Databricks App container, binding to `DATABRICKS_APP_PORT`. `entrypoint.sh` sets `OPENCLAW_HOME` / `OPENCLAW_STATE_DIR` under `/home/app`, symlinks bundled `openclaw.json` into the state dir, then runs `sync_volume.py` before boot. The same script pushes state back on shutdown (signal trap) and on a periodic heartbeat.

**Bundled behavior (see `openclaw.json`):** default model is `databricks-openai/databricks-gpt-5-4`; Telegram uses an allowlist (`TELEGRAM_ALLOWED_USER_ID`); gateway auth is token-based (`GATEWAY_TOKEN`); the `databricks-unity-catalog` skill is enabled; extra skills can be loaded from `.openclaw/workspace/skills` on the volume after sync.

## Project Structure

```
lakeclaw/
├── databricks.yml              # Bundle: app resource, secrets, UC volume binding, targets
├── README.md
├── app.yaml                    # App command (entrypoint.sh) and env → secret/volume mapping
├── openclaw.json               # OpenClaw config (models, Telegram, gateway auth, skills)
├── entrypoint.sh               # Sets OPENCLAW_* paths, initial pull, heartbeat, npm start
├── sync_volume.py              # Pull tree from volume → /home/app; push selected files back
└── package.json                # openclaw CLI; start script uses DATABRICKS_APP_PORT
```

## Prerequisites

- [Databricks CLI](https://docs.databricks.com/dev-tools/cli/index.html) with bundle support (0.200+; project tested with recent CLI releases)
- A Databricks workspace with [Apps](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html) enabled
- A Unity Catalog volume for state persistence (create empty or seed with prior `.openclaw` content)
- A Databricks secret scope holding the keys referenced by the bundle (see below)
- **AI Gateway:** workspace must be able to reach Databricks AI Gateway; `openclaw.json` expects a URL shaped like `https://${DATABRICKS_WORKSPACE_ID}.ai-gateway.cloud.databricks.com/openai/v1` (placeholder is resolved by OpenClaw from the runtime environment)

## Secrets Setup

Create the secret scope and populate the required secrets. This is a one-time setup:

```bash
databricks secrets create-scope lakeclaw
databricks secrets put-secret lakeclaw gateway-passphrase
databricks secrets put-secret lakeclaw telegram-bot-token
databricks secrets put-secret lakeclaw openai-key
databricks secrets put-secret lakeclaw databricks-token
```

| Secret key (in scope)   | Injected as / used for |
| ----------------------- | ---------------------- |
| `gateway-passphrase`    | `GATEWAY_TOKEN` — gateway HTTP auth (`openclaw.json` `gateway.auth.token`) |
| `telegram-bot-token`    | `TELEGRAM_BOT_TOKEN` |
| `databricks-token`      | `DATABRICKS_TOKEN` — Workspace API client in `sync_volume.py` and AI Gateway auth for the default model |

**Telegram allowlist:** `openclaw.json` sets `dmPolicy` to `allowlist` and reads `TELEGRAM_ALLOWED_USER_ID`. That value is set in `app.yaml` (currently a literal); change it there (or switch to a secret) for your Telegram user ID.

## Configuration

The bundle uses variables that can be overridden per target or at deploy time:

| Variable           | Default                                      | Description |
| ------------------ | -------------------------------------------- | ----------- |
| `secret_scope`     | `lakeclaw`                                   | Secret scope name used by app resources |
| `lakeclaw_volume`  | `/Volumes/catalog/schema/lakeclaw_volume`    | UC volume **path** (must match how you created the volume) |

Update the defaults in `databricks.yml` or override at deploy time:

```bash
databricks bundle deploy -var='lakeclaw_volume=/Volumes/my_catalog/my_schema/lakeclaw_volume'
```

## Deployment

### Validate

```bash
databricks bundle validate
```

### Deploy

```bash
# Deploy to dev (default target)
databricks bundle deploy

# Deploy to production
databricks bundle deploy -t prod
```

### Start the App

Deploying the bundle creates the app resource but does not start it. To start:

```bash
databricks bundle run lakeclaw_app
```

### View logs

```bash
databricks apps logs lakeclaw-dev
```

Use `lakeclaw-prod` when deployed to the `prod` target. Adjust if you change app names in `databricks.yml`.

### Tear Down

```bash
databricks bundle destroy
```

## State persistence

OpenClaw state lives under `/home/app/.openclaw` (`OPENCLAW_STATE_DIR`). The volume path comes from the app resource `lakeclaw-volume` (Unity Catalog), exposed to the container as `MY_UC_VOLUME_PATH`.

`sync_volume.py` behavior:

1. **On startup (before gateway)** — recursively downloads the volume tree into `/home/app`, preserving paths under `.openclaw/` (and other paths relative to `APP_HOME`).
2. **Every hour** — background loop in `entrypoint.sh` runs `sync_volume.py --push`.
3. **On shutdown** — `SIGTERM` / `SIGINT` runs a final `sync_volume.py --push`.

**Push rules:** uploads files under `.openclaw` with extensions `.md`, `.json`, `.jsonl`, `.db`, `.key`, `.yaml`. Skips `openclaw.json` and symlinks (the bundled config is symlinked from `/app/python/source_code` and should not be written back as content).

**Client:** `WorkspaceClient()` from `databricks-sdk` uses standard Databricks credential resolution; this app supplies `DATABRICKS_TOKEN` for Files API sync and for the bundled AI Gateway config.

## Targets

| Target | Mode          | App name        | Notes |
| ------ | ------------- | --------------- | ----- |
| `dev`  | `development` | `lakeclaw-dev`  | Default target |
| `prod` | `production`  | `lakeclaw-prod` | `databricks.yml` grants `CAN_MANAGE` on the app to `${DATABRICKS_CLIENT_ID}` (set in your deploy environment when using this block) |
