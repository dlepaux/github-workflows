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

## Caller conventions

Every consumer repo must:

1. Have a single `.github/workflows/ci.yml`
2. Set top-level `permissions: contents: write, issues: write, pull-requests: write, packages: write`
3. Use `secrets: inherit` for release and docker jobs (deploy uses explicit `webhook-key`)
4. Add `LABEL org.opencontainers.image.source=https://github.com/dlepaux/<repo>` in the **final stage** of the Dockerfile
5. Have a `package.json` with `semantic-release` as a devDependency (even non-Node repos)

## Concurrency

All consumers use global queue concurrency — one run at a time, no cancellation:

```yaml
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: false
```

## Architecture decisions

- **Quality checks stay inline** — too varied per repo (different languages, containers, service deps) to abstract
- **Self-hosted for Docker builds** — saves GitHub Actions minutes (3000/month limit), ARM64 native on Pi5/Rock5B+
- **GitHub-hosted for release/deploy** — lightweight jobs (~30s), not worth occupying a runner
- **Per-job Docker credential isolation** — `DOCKER_CONFIG=$(mktemp -d)` prevents cross-repo token contamination on shared self-hosted runners
