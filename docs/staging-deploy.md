# Staging Deployments

Sandcastle staging deploys are intentionally disposable. A pull request with the
`deploy:staging` label deploys that PR head commit to the Oracle VPS, removes
the previous Sandcastle staging arena, and runs a fresh Docker-in-Docker smoke.

## Trigger

The GitHub Actions deploy job runs only when all of these are true:

- the event is a pull request event
- the PR has the `deploy:staging` label
- the PR is not a draft
- the PR branch belongs to this repository
- all required CI jobs in the workflow passed

Adding `deploy:staging` starts the first deployment. Pushing more commits to the
same PR redeploys automatically while the label remains present. To pause
staging deploys for that PR, remove the label.

The deployed code is the PR head commit, not `main` and not GitHub's synthetic
merge commit.

## GitHub Environment

Create a protected GitHub environment named `staging`. Store these secrets in
that environment:

- `STAGING_SSH_HOST`: VPS hostname or IP address
- `STAGING_SSH_USER`: SSH deploy user
- `STAGING_SSH_PRIVATE_KEY`: private key for the deploy user
- `STAGING_SSH_KNOWN_HOSTS`: pinned SSH host key line from `ssh-keyscan`
- `STAGING_OPERATOR_TOKEN`: organizer token for staging match controls
- `STAGING_CHECKER_SECRET`: checker master secret for staging
- `STAGING_TEAM_TOKEN_PATTERN`: team token pattern containing `{team}`
- `OPENAI_API_KEY`: optional, enables OpenAI-backed agents and challenge generation
- `GEMINI_API_KEY`: optional, enables Gemini-backed agents and challenge generation

Optional environment variables:

- `STAGING_SSH_PORT`: SSH port, default `22`
- `STAGING_DEPLOY_PATH`: remote checkout path, default `/opt/sandcastle-staging`
- `SANDCASTLE_STAGING_TEAMS`: team count, default `2`
- `SANDCASTLE_STAGING_TIMEOUT`: startup timeout in seconds, default `240`

Keep the staging environment protected so an operator approves deployments
before secrets are exposed to PR code.

## VPS Prerequisites

The Oracle VPS should have:

- native Linux Docker Engine
- Docker Compose plugin
- Bash, `rsync`, `sudo`, and standard GNU userland tools
- deploy-user Docker access
- passwordless permission for:

```bash
sudo -n ./scripts/firewall-preflight.sh --apply
```

The deploy user must be able to create and write `STAGING_DEPLOY_PATH`. If the
path is under `/opt`, create it once and assign ownership to the deploy user.

## Deploy Lifecycle

The workflow runs `./scripts/staging-deploy.sh` from the checked-out PR head.
The script:

1. validates all required staging secrets and variables
2. writes the SSH key and known-hosts data to temporary files
3. creates the remote staging directory
4. syncs the PR checkout with `rsync --delete`, preserving remote runtime data
5. runs the remote deployment in `STAGING_DEPLOY_PATH`

On the VPS, the deployment:

1. runs `./scripts/cleanup.sh --remove-generated`
2. applies the firewall bridge-netfilter preflight
3. writes staging-only values into `config/arena.env`
4. runs `./scripts/staging-dind-smoke.sh`
5. prints `./scripts/arena.sh status --format tsv`

## Cleanup Safety

Cleanup is scoped to Sandcastle resources only. It removes containers, volumes,
networks, images, and generated team workspaces selected by Sandcastle labels or
Sandcastle-specific names such as `teamN-dind`,
`sandcastle_teamN-dind-data`, and `sandcastle_teamN-dind-run`.

It does not run broad Docker prune commands.

## Manual Redeploy And Rollback

To redeploy the same PR, remove and re-add `deploy:staging`, or push a new
commit while the label is present.

To roll staging back to another branch, open or update a PR from that branch and
apply `deploy:staging`. Staging is single-tenant, so the newest successful
labeled PR deployment replaces the previous one.

To stop staging manually on the VPS:

```bash
cd /opt/sandcastle-staging
./scripts/cleanup.sh --remove-generated
```

To inspect staging:

```bash
cd /opt/sandcastle-staging
./scripts/arena.sh status --format tsv
docker compose logs --tail=120
```
