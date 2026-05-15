# github-workflows

Shared reusable GitHub Actions workflows for all `dlepaux/*` repos.

## Workflows

### release.yml

Runs `npx semantic-release` with version detection. Requires `package.json` with semantic-release as a devDependency.

| | |
|---|---|
| **Runner** | `ubuntu-latest` |
| **Inputs** | `node-version` (default: `22`) |
| **Outputs** | `released` (true/false), `version` (without `v` prefix) |
| **Secrets** | Inherited (`GITHUB_TOKEN`) |

```yaml
release:
  needs: quality
  if: github.event_name == 'push' && github.ref == 'refs/heads/main'
  uses: dlepaux/github-workflows/.github/workflows/release.yml@main
  secrets: inherit
```

### docker-build-push.yml

Raw `docker build` + `docker push` on self-hosted ARM64 runners. Uses per-job credential isolation to prevent cross-repo contamination on shared runners.

| | |
|---|---|
| **Runner** | `[self-hosted, homelab]` |
| **Inputs** | `image-name` (required), `version`, `build-args` |
| **Secrets** | Inherited (`GITHUB_TOKEN`) |
| **Tags** | `:latest`, `:sha-<7chars>`, `:<version>` (if provided) |

```yaml
docker:
  needs: [quality, release]
  if: needs.release.outputs.released == 'true'
  uses: dlepaux/github-workflows/.github/workflows/docker-build-push.yml@main
  with:
    image-name: ghcr.io/${{ github.repository }}
    version: ${{ needs.release.outputs.version }}
  secrets: inherit
```

### docker-build-push-buildx.yml

Docker build with buildx on GitHub-hosted runners. For repos needing x86 images or GHA layer caching.

| | |
|---|---|
| **Runner** | `ubuntu-latest` |
| **Inputs** | `image-name` (required), `version`, `platforms` (default: `linux/arm64`), `free-disk-space` (default: `false`) |
| **Secrets** | Inherited (`GITHUB_TOKEN`) |

```yaml
docker:
  needs: [quality, release]
  if: needs.release.outputs.released == 'true'
  uses: dlepaux/github-workflows/.github/workflows/docker-build-push-buildx.yml@main
  with:
    image-name: ghcr.io/${{ github.repository }}
    version: ${{ needs.release.outputs.version }}
    platforms: linux/amd64
    free-disk-space: true
  secrets: inherit
```

### wake-compute.yml

Wakes srv-compute via WoL before X64 runner jobs. Runs on srv-core (ARM64, always on), calls the WoL HTTP relay on the Pi 3, polls node_exporter until srv-compute is reachable.

| | |
|---|---|
| **Runner** | `[self-hosted, homelab, ARM64]` |
| **Secrets** | None — token read from `/opt/github-runner/.wol-token` (Ansible-managed) |

Used internally by `docker-build-push-native.yml`. Can also be called directly:

```yaml
wake:
  uses: dlepaux/github-workflows/.github/workflows/wake-compute.yml@main
```

### deploy.yml

Webhook deploy with configurable retry logic.

| | |
|---|---|
| **Runner** | `ubuntu-latest` |
| **Inputs** | `webhook-url` (default: webhook.lepaux.com), `retries` (default: `3`), `retry-delay` (default: `30`s) |
| **Secrets** | `webhook-key` (explicit, not inherited) |

```yaml
deploy:
  needs: docker
  uses: dlepaux/github-workflows/.github/workflows/deploy.yml@main
  secrets:
    webhook-key: ${{ secrets.WEBHOOK_DEPLOY_KEY }}
```

### rust-service.yml

Full CI pipeline for Rust services that ship a binary + Docker image to GHCR and deploy via homelab-webhook. Jobs run on self-hosted ARM64 runners. Used by: gordon-data, gordon-manager, gordon-risk, gordon-bot, gordon-executor, gordon-migrate.

| | |
|---|---|
| **Runner** | `[self-hosted, Linux, ARM64, <runner_host>]` |
| **Cache** | Persistent `CARGO_TARGET_DIR` + `CARGO_HOME` from `runner.env` — no `actions/cache` |
| **Jobs** | `build-and-gate`, `test-coverage`, `asyncapi-drift` (optional), `trivy`, `release`, `docker` |

**Inputs:**

| Input | Type | Default | Description |
|---|---|---|---|
| `binary_name` | string | required | Rust binary name, e.g. `gordon-manager` |
| `service_port` | number | required | HTTP port — `SERVICE_PORT` Docker build-arg |
| `has_openapi` | boolean | `false` | Enable OpenAPI drift check in `build-and-gate` |
| `has_asyncapi` | boolean | `false` | Enable `asyncapi-drift` job |
| `coverage_floor` | number | `70` | Workspace line coverage gate (percent) |
| `deploy_webhook` | boolean | `true` | Trigger homelab-webhook deploy step |
| `runner_host` | string | `homelab` | Runner label — `homelab` (float), `host-srv-core`, `host-srv-apps` (pinned) |

**Secrets:** `HOMELAB_WEBHOOK_URL`, `WEBHOOK_DEPLOY_KEY` (only consumed when `deploy_webhook: true`).

**Caller requirements:** Dockerfile must have a `runtime-prebuilt` target that accepts `PREBUILT_BINARY` and `SERVICE_PORT` build-args. See `caller-examples/gordon-manager-ci.yml`.

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
  workflow_dispatch:
jobs:
  ci:
    uses: dlepaux/github-workflows/.github/workflows/rust-service.yml@v1.0.0
    with:
      binary_name: gordon-risk
      service_port: 8082
      has_openapi: true
      has_asyncapi: true
    secrets:
      HOMELAB_WEBHOOK_URL: ${{ secrets.HOMELAB_WEBHOOK_URL }}
      WEBHOOK_DEPLOY_KEY: ${{ secrets.WEBHOOK_DEPLOY_KEY }}
```

### rust-crate.yml

CI pipeline for kellnr-published Rust library crates. No Docker, no deploy. Used by: gordon-protocol, gordon-platform, gordon-kernel, gordon-domain, gordon-bus, gordon-strategy, gordon-exchange, gordon-test-db.

| | |
|---|---|
| **Runner** | `[self-hosted, Linux, ARM64, <runner_host>]` |
| **Cache** | Persistent `CARGO_TARGET_DIR` + `CARGO_HOME` from `runner.env` — no `actions/cache` |
| **Jobs** | `build-and-gate`, `test-coverage`, `trivy`, `release`, `publish` |

**Inputs:**

| Input | Type | Default | Description |
|---|---|---|---|
| `crate_name` | string | required | Crate name as in `Cargo.toml`, e.g. `gordon-protocol` |
| `publish_to_kellnr` | boolean | `true` | Run `cargo publish --registry kellnr` after release |
| `runner_host` | string | `homelab` | Runner label — same semantics as `rust-service.yml` |

**Secrets:** none — kellnr token lives in `CARGO_HOME/config.toml` on the runner (Ansible-managed, not a GitHub secret).

**Note:** no Postgres container. Library crates must not have integration tests that require a live database. Tests that genuinely need Postgres belong in the calling service's suite.

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
  workflow_dispatch:
jobs:
  ci:
    uses: dlepaux/github-workflows/.github/workflows/rust-crate.yml@v1.0.0
    with:
      crate_name: gordon-protocol
```

## Caller conventions

Every consumer repo must:

1. Have a single `.github/workflows/ci.yml`
2. Set top-level `permissions: contents: write, issues: write, pull-requests: write, packages: write`
3. Use `secrets: inherit` for release and docker jobs (deploy uses explicit `webhook-key`)
4. Add `LABEL org.opencontainers.image.source=https://github.com/dlepaux/<repo>` in the **final stage** of the Dockerfile
5. Have a `package.json` with `semantic-release` as a devDependency (even non-Node repos)

## Concurrency

Existing Docker/release workflows use a global queue (one run at a time per repo):

```yaml
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: false
```

`rust-service.yml` and `rust-crate.yml` use **per-ref** concurrency instead. Different refs (e.g. a PR branch and main) run in parallel; concurrent runs on the same ref queue rather than overlap — the persistent `CARGO_TARGET_DIR` is per-repo, not per-ref, and concurrent cargo builds contend on advisory locks.

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false
```

## Architecture decisions

- **Quality checks stay inline** — too varied per repo (different languages, containers, service deps) to abstract
- **Self-hosted for Docker builds** — saves GitHub Actions minutes (3000/month limit), ARM64 native on Pi5/Rock5B+
- **GitHub-hosted for release/deploy** — lightweight jobs (~30s), not worth occupying a runner
- **Per-job Docker credential isolation** — `DOCKER_CONFIG=$(mktemp -d)` prevents cross-repo token contamination on shared self-hosted runners
