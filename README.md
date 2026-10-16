# Devoker

Texas Hold'em with friends: a FastAPI backend, a PostgreSQL database and a React frontend.
Delivered by a pipeline that runs from `git push` to a running, monitored Kubernetes deployment.

On every push to `main`, GitHub Actions runs the tests and four kinds of security scan, then builds
both images and scans them again. Only images with no fixable HIGH/CRITICAL vulnerabilities reach
GHCR. The pipeline then commits the new image tag to the Helm chart, and Argo CD, running inside the
cluster, deploys it. Prometheus scrapes the backend's `/metrics`, Grafana shows a Devoker dashboard,
and the frontend scales on CPU. Terraform can stand up the same stack's AWS side: a VPC and an EKS cluster.

## Contents

[Where each requirement lives](#where-each-requirement-lives) · [Architecture](#architecture) · [Technologies](#technologies) · [Run the app](#run-the-app) ·
[Docker](#docker) · [Kubernetes](#kubernetes) · [Helm](#helm) · [CI/CD](#cicd) ·
[DevSecOps](#devsecops) · [GitOps](#gitops) · [Monitoring](#monitoring) · [Terraform](#terraform) ·
[Troubleshooting](#troubleshooting) · [Screenshots](#screenshots) · [Lessons learned](#lessons-learned) ·
[API](#api)

## Where each requirement lives

| Requirement | Where |
|---|---|
| FastAPI app, `/health`, REST endpoints (GET/POST/PUT/DELETE) | `backend/app/main.py`, [API](#api) |
| PostgreSQL + Alembic migration | `backend/app/models.py`, `backend/alembic/versions/0001_create_rooms_and_hands.py` |
| React UI | `frontend/src/App.jsx` |
| pytest (34 tests, test DB) | `backend/pytest.ini`, `backend/tests/` (`conftest.py` builds a throwaway SQLite DB with `alembic upgrade head`) |
| Dockerfiles (multi-stage, non-root) | `backend/Dockerfile`, `frontend/Dockerfile` |
| Compose, 3 services | `docker-compose.yml` → http://localhost:3000 |
| Pipeline (pytest, build, GHCR, SHA tags) | `.github/workflows/ci.yaml` |
| Trivy on both images, fails on HIGH/CRITICAL | `build-scan-push` job, [DevSecOps](#devsecops) |
| Terraform: VPC, 2 public subnets, EKS + node group | `terraform/vpc.tf`, `terraform/eks.tf`, `terraform/terraform.tfvars.example` |
| Namespace, Helm chart, Ingress `/` and `/api` | `k8s/namespace.yaml`, `helm/devoker/` |
| `/metrics`, Prometheus scrape, Grafana panels | `backend/app/main.py`, `helm/devoker/templates/servicemonitor.yaml`, `helm/devoker/dashboards/devoker.json` |

## Architecture

```
┌──────────────────── Kubernetes (minikube) · namespace "devoker" ────────────────────┐
│                                                                                     │
│  browser ─► devoker.localhost ─► Ingress ─┬─ /     ─► Service frontend ─► nginx pods    │
│                                       │                               2..5, HPA     │
│                                       └─ /api  ─► Service backend ─► FastAPI pods   │
│                                                                       2 replicas    │
│                                                                           │         │
│                                                  Service postgres ─► Postgres 16 ◄──┘
│                                                                       PVC 1 Gi      │
│  ConfigMap (blinds, chips) ─► backend    Secret devoker-db ─► backend + postgres    │
│  PVC (nginx access log)   ─► frontend    ServiceMonitor ─► Prometheus scrapes /metrics│
└─────────────────────────────────────────────────────────────────────────────────────┘

git push ─► GitHub Actions: tests ─► SAST · SCA · secrets ─► build ─► Trivy gate
                            ─► push to GHCR (:sha) ─► bot commits new tag to helm values.yaml
                                                                    │
Argo CD (inside the cluster) polls git, sees the new tag, syncs ◄────┘

Prometheus + Grafana (kube-prometheus-stack): app metrics, pods, CPU, the HPA and two alert rules
Terraform (on the side): AWS VPC + EKS cluster, ECR repos, an encrypted S3 bucket
```

### State lives in Postgres, so the backend scales

Every room is one row in `rooms` (the table as JSONB plus a `version` counter), and every finished
hand is a row in `hands`. No replica keeps anything in memory, so:

| Decision | Why |
|---|---|
| Backend runs **2 replicas with rolling updates** | Any replica can serve any player. A deploy replaces pods one at a time and games carry on. The first version of this app kept rooms in memory, which forced 1 replica and `Recreate` ([incident 2](troubleshooting/02-split-brain.md) shows what went wrong with 2). |
| Each change runs under **`SELECT ... FOR UPDATE`** on the room's row | Two players acting at once on different replicas queue on the row lock instead of overwriting each other. |
| **Live updates poll the `version` column** every 0.5 s | Each player's Server-Sent Events stream sends the table again when the version moves, whichever replica made the change. Switch to Postgres `LISTEN/NOTIFY` if thousands of players connect. |
| **Migrations run at startup**, behind a Postgres advisory lock | Two replicas starting together take turns, and the second sees the schema is already current. An init container waits for Postgres first. |
| Every proxy has **buffering off and a 1 h read timeout** | An event stream is a long-lived HTTP response. The server sends a ping every 25 s. |

## Technologies

| Area | Tools |
|---|---|
| App | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, React 19, Vite 8 |
| Tests | pytest, httpx (FastAPI `TestClient`) |
| Containers | Docker multi-stage builds, python:3.12-slim (backend), nginx-unprivileged (frontend), Docker Compose |
| Orchestration | Kubernetes (minikube), ingress-nginx, metrics-server, Helm 4 |
| CI/CD | GitHub Actions, GHCR |
| Security | bandit (SAST), pip-audit + npm audit + Trivy fs (SCA and misconfig), Gitleaks (secrets), Trivy image |
| GitOps | Argo CD |
| Monitoring | prometheus-fastapi-instrumentator, kube-prometheus-stack (Prometheus, Grafana, kube-state-metrics), ServiceMonitor, PrometheusRule alerts, fortio load test |
| Infrastructure as code | Terraform with the AWS provider (VPC, EKS, ECR, S3) |

## Repository layout

| Folder | Contents |
|---|---|
| `backend/` | FastAPI app, Alembic migrations, pytest suite, Dockerfile |
| `frontend/` | React app, Dockerfile, `nginx.conf` |
| `docker-compose.yml` | Postgres, backend and frontend for local runs |
| `k8s/` | `namespace.yaml` |
| `helm/devoker/` | The Helm chart (backend, frontend, Postgres, Ingress, HPA, ServiceMonitor, dashboard); `helm/values-local.yaml` for locally built images |
| `.github/workflows/ci.yaml` | The pipeline |
| `security/` | `.trivyignore` with the reason for each accepted finding |
| `gitops/` | The Argo CD `Application` |
| `monitoring/` | Stack values, alert rules, load test results |
| `terraform/` | AWS resources |
| `troubleshooting/` | Five incidents with real output |
| `legacy-go-backend/` | The first backend, in Go with in-memory state, kept for reference. Nothing builds or deploys it. |
| `scripts/smoke.py` | Plays one hand through the public API; used to verify every stage |

## Run the app

Without containers, for development (Postgres from compose):

```bash
docker compose up -d postgres
cd backend && python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -v                                        # 34 tests, uses a throwaway SQLite DB
DATABASE_URL=postgresql+psycopg://devoker:devoker@localhost:5432/devoker alembic upgrade head
uvicorn app.main:app --reload                    # :8000, API docs at /docs
cd frontend && npm install && npm run dev        # :5173, proxies /api to :8000
```

(Compose doesn't publish Postgres's port by default; add `ports: ["5432:5432"]` to the `postgres` service for this.)

Configuration is by environment variable: `DATABASE_URL`, `STARTING_CHIPS` (1000), `SMALL_BLIND` (10), `BIG_BLIND` (20).

## Docker

```bash
docker compose up --build                       # http://localhost:3000
python3 scripts/smoke.py                        # plays one hand against localhost:3000
docker compose exec postgres psql -U devoker -c 'select room_code, number, pot from hands'
```

| Image | Size | Base | Runs as |
|---|---|---|---|
| backend | 257 MB | `python:3.12-slim`, dependencies in a venv copied from the build stage | UID 10001 |
| frontend | 60 MB | `nginxinc/nginx-unprivileged:stable-alpine` | UID 101 |

- **Multi-stage builds.** The backend's build stage installs the dependencies into `/venv`; only the
  venv and the app code are copied into the runtime image. The frontend's Node build stage doesn't ship at all.
- **Layer caching.** Dependency manifests (`requirements.txt`, `package-lock.json`) are copied before source.
- **Three services.** `postgres` has a healthcheck, and `backend` waits for it to be healthy
  (`depends_on: condition: service_healthy`) before running its migration.
- **The frontend's nginx** serves the React build, falls back to `index.html` for client routes, and
  proxies `/api/` to the `backend` service with buffering off. Its access log logs `$uri` rather than
  the full request, because the event-stream URL carries the player's secret token. Uvicorn's access
  log is off for the same reason.
- **Both `USER`s are numeric**, because Kubernetes `runAsNonRoot` can't verify a user name
  ([incident 1](troubleshooting/01-non-numeric-user.md)).

## Kubernetes

```bash
minikube start --cpus 4 --memory 6g
minikube addons enable ingress && minikube addons enable metrics-server
docker build -t devoker-backend:dev backend && docker build -t devoker-frontend:dev frontend
minikube image load devoker-backend:dev && minikube image load devoker-frontend:dev
kubectl apply -f k8s/namespace.yaml
kubectl -n devoker create secret generic devoker-db --from-literal=password="$(openssl rand -hex 16)"
helm upgrade --install devoker helm/devoker -n devoker -f helm/values-local.yaml
kubectl -n devoker get pods
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8081:80 &
python3 scripts/smoke.py http://localhost:8081 devoker.localhost
```

Then open http://devoker.localhost:8081 in a browser. Browsers resolve any `*.localhost` name to 127.0.0.1, so no `/etc/hosts` entry is needed.

| Object | Devoker's use |
|---|---|
| Namespace | `devoker` (`k8s/namespace.yaml`) |
| Deployment ×3 | backend (2 replicas, rolling update), frontend (count owned by the HPA, 2–5), postgres (1, `Recreate`) |
| Service ×3, all ClusterIP | `backend` (port 8000), `frontend` (8080), `postgres` (5432) |
| ConfigMap | `STARTING_CHIPS`, `SMALL_BLIND`, `BIG_BLIND`, loaded with `envFrom` |
| Secret | `devoker-db`, the Postgres password, created with kubectl and never committed |
| Ingress | `devoker.localhost`: `/api` → backend, `/` → frontend, with SSE annotations |
| HPA | frontend 2–5 replicas at 50% CPU |
| PVC ×2 | Postgres data (1 Gi), nginx access log (1 Gi) |
| Probes | backend readiness `/ready` (runs `SELECT 1`), liveness `/health`; frontend `/` |
| Init container | `wait-for-postgres` runs `pg_isready` so the backend doesn't crash-loop on a fresh install |

Hardening: every container drops all Linux capabilities, disallows privilege escalation and runs as
non-root with a read-only root filesystem. Postgres runs as UID 70 and writes only to its PVC and `emptyDir`s for `/tmp` and `/var/run/postgresql`.

Postgres's PVC is `ReadWriteOnce` and the Deployment uses `Recreate`, so an old and a new Postgres
pod never mount the volume together. One Postgres pod is a single point of failure; production would
use a managed database (RDS) or an operator such as CloudNativePG.

## Helm

```bash
helm upgrade --install devoker helm/devoker -n devoker -f helm/values-local.yaml
helm upgrade devoker helm/devoker -n devoker -f helm/values-local.yaml --set game.bigBlind=50
helm list -n devoker
helm history devoker -n devoker
helm rollback devoker <revision> -n devoker
```

- **`values.yaml`** holds the image repositories and tags, backend replicas, resources, autoscaling,
  game settings, the Postgres image and the *name* of its Secret, the ingress host, and `monitoring.enabled`.
- **A `checksum/config` annotation** on the backend pod rolls it when the ConfigMap changes.
  Environment variables are read only at start, so without it a blind change would do nothing.
- **The frontend Deployment omits `replicas` while the HPA is enabled**, so a sync doesn't fight the autoscaler.
- **`monitoring.enabled`** adds the ServiceMonitor and the Grafana dashboard ConfigMap. Turn it off
  on a cluster without the Prometheus Operator CRDs (a plain EKS cluster, for example).

## CI/CD

`.github/workflows/ci.yaml`, linted with `actionlint`:

```
           ┌─ test-backend   pip install · pytest -v
 push ─────┼─ test-frontend  npm ci · npm run build
           ├─ sast           bandit (fails on HIGH)
           ├─ sca            pip-audit · npm audit (HIGH) · trivy fs (vulns + misconfig)
           └─ secrets        gitleaks over the full history
                                        │ all five must pass
                     build-scan-push   matrix: backend, frontend
                       docker build → trivy image gate → push ghcr.io/imajij/devoker-*:<sha> (amd64 + arm64)
                                        │ main only
                     deploy            yq sets both tags in helm/devoker/values.yaml → commit → push
```

- **Images are tagged by commit SHA**, so the running version is traceable and Argo sees a change on every deploy.
- **Images are scanned before they're pushed**, so a vulnerable image never reaches the registry.
- **Images are multi-arch** (amd64 for runners and cloud clusters, arm64 for Apple Silicon minikube).
  The first amd64-only build would have crashed on the ARM minikube with `exec format error`. The
  backend's arm64 image is built under QEMU; the React build runs once on the runner's own platform
  (`--platform=$BUILDPLATFORM`) because its output doesn't depend on CPU.
- **Permissions are least-privilege per job.** Only build gets `packages: write`; only deploy gets `contents: write`.
- **The deploy commit doesn't loop.** It includes `[skip ci]`, and the trigger ignores `helm/devoker/values.yaml`.
- **A `concurrency` group** stops two pushes racing to commit tags.
- **The image registry is hard-coded lowercase** (`ghcr.io/imajij`). Image names must be lowercase,
  and the GitHub owner is `imajij`.

## DevSecOps

| Scan | Tool | Looks at | Gate |
|---|---|---|---|
| SAST | bandit | our Python code (`backend/app`) | fails on HIGH |
| SCA | pip-audit | Python dependencies in `requirements.txt` | fails on any |
| SCA | npm audit | frontend dependencies | fails on HIGH |
| SCA and misconfig | Trivy fs | lockfiles, Kubernetes manifests, Helm, Dockerfiles, Terraform | fails on HIGH/CRITICAL |
| Secrets | Gitleaks | every commit in history | fails on any |
| Image | Trivy image | OS packages and binaries in each image | fails on fixable HIGH/CRITICAL |

**Trivy image result.** Both images pass the gate: 0 fixable HIGH or CRITICAL vulnerabilities.
The gate runs *before* the push with `--exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed`, so a
failing image never reaches GHCR and Argo CD can never deploy it. `--ignore-unfixed` skips findings
with no patched package yet: failing the build on those would block every deploy without making
anything safer. They come back the day a fix ships. Debian's `-slim` and Alpine bases keep the
package count low, which is most of why the scan is clean. To see the gate fail, pin an old base
such as `python:3.9-slim-bullseye` and push: Trivy lists the CVEs and the job goes red.

What the gates found while this was built, and what changed:

1. **Predictable shuffle.** The first version of the deck used a seeded random generator, which
   gosec flagged as HIGH (G404) in the Go backend. For a card game it's a real flaw: whoever works out
   the generator's state can predict the cards. The Python deck shuffles with `secrets.SystemRandom`,
   the operating system's cryptographic source, and bandit stays clean at HIGH.
2. **An outdated toolchain.** govulncheck found 8 standard-library CVEs in the Go backend's minimum
   Go version. That was the reason CI pinned the latest patch release; for Python, pip-audit plays the same role.
3. **Trivy KSV-0014 (HIGH): the frontend root filesystem was writable.** It's now read-only, with `/tmp` as an `emptyDir`.
4. **Trivy AWS-0132 (HIGH): the S3 bucket isn't encrypted with a customer-managed KMS key.**
   Accepted in `security/.trivyignore` with the reason written there. The bucket uses SSE-S3 (AES256),
   and a KMS key costs ~$1/month and can't be deleted for 7+ days, which is wrong for a lab destroyed the same day.
5. **Trivy AWS-0039, 0040, 0041, 0164 on the EKS stack** (no secrets envelope encryption, public API
   endpoint, open to 0.0.0.0/0, subnets that assign public IPs). Accepted in `.trivyignore` for a
   one-hour lab cluster with no NAT gateway; `cluster_public_access_cidrs` narrows the endpoint to one IP.

**Proving the gate.** On a branch, `cd frontend && npm i lodash@4.17.20`, then push. `npm audit`
reports lodash HIGH (GHSA-35jh-r3h4-6jhm, command injection) and the pipeline goes red. That was
checked locally (`npm audit` exits 1). Revert afterwards. To prove the secret scan, commit a fake AWS
key on a throwaway branch.

Out-of-band secrets: the Postgres password (`devoker-db`) and Grafana's admin password are created in the cluster,
never committed. The trade-off is that Argo CD doesn't manage them. Sealed Secrets or External Secrets would fix that.

## GitOps

```bash
kubectl -n devoker create secret generic devoker-db --from-literal=password="$(openssl rand -hex 16)"
kubectl create ns argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
helm uninstall devoker -n devoker          # Argo owns the release from now on
kubectl apply -f gitops/devoker-app.yaml   # the last manual apply
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
kubectl -n argocd port-forward svc/argocd-server 8443:443   # https://localhost:8443, user admin
```

The Application tracks `helm/devoker` on `main` with automated sync, `prune` (delete what git no
longer has), `selfHeal` (undo manual edits) and `CreateNamespace`. CI can't reach a laptop cluster,
so it never deploys directly. It commits the new tag, and Argo pulls it (it polls every ~3 minutes).

- **Roll back** with `git revert` of the deploy commit, not `helm rollback`. Git stays the record of what runs.
- **Drift test:** `kubectl -n devoker scale deploy/backend --replicas=5`. Argo returned it to the
  value in git within 2 seconds.
- **Live demo:** change something visible (for example `game.bigBlind` or a frontend label), push to
  `main`, and watch the pipeline go green, the bot commit the new SHA tags, Argo sync, and
  `kubectl -n devoker get pods -w` roll the backend pods one at a time while a game keeps going.

## Monitoring

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install mon prometheus-community/kube-prometheus-stack -n monitoring --create-namespace -f monitoring/values.yaml
kubectl apply -f monitoring/devoker-alerts.yaml
kubectl -n monitoring get secret mon-grafana -o jsonpath='{.data.admin-password}' | base64 -d
kubectl -n monitoring port-forward svc/mon-grafana 3000:80
```

The backend exposes Prometheus metrics at `/metrics`: request rate, latency histograms and status
codes per endpoint (prometheus-fastapi-instrumentator), plus two of its own:

- `devoker_hands_played_total`: poker hands played to the end.
- `devoker_open_event_streams`: players connected to a table, per replica.

The chart's ServiceMonitor tells Prometheus to scrape every backend pod; *Status → Targets* shows both
UP. The chart also ships the **Devoker** Grafana dashboard as a ConfigMap that Grafana's sidecar loads,
with six panels: requests per second by endpoint, p95 latency, responses by status, players
connected, hands played per minute, and backend pods up.

`monitoring/values.yaml` sets `serviceMonitorSelectorNilUsesHelmValues: false` so Prometheus picks up
ServiceMonitors from other releases, and the Grafana sidecar searches every namespace. Alert rules:

- **DevokerBackendRestarted** (warning): a backend restart drops that pod's event streams. Clients reconnect, but a loop of restarts is a bug.
- **DevokerFrontendAtMaxReplicas** (warning): the HPA has been at its ceiling for 10 minutes.

**Load test** ([monitoring/loadtest.md](monitoring/loadtest.md)): fortio inside the cluster, 50
connections for 150 s through the Ingress. It made 2,606,625 requests, all HTTP 200, at 17,375 req/s
and 2.9 ms average. The frontend went from 2 to 5 replicas within 20 s and back to 2 after the load
stopped. (That run was against the first, Go backend; the frontend and HPA are unchanged.)

Logs: `kubectl -n devoker logs deploy/backend -f`, and the nginx access log on the PVC:
`kubectl -n devoker exec deploy/frontend -- tail /var/log/devoker/access.log`.

## Terraform

`terraform/` creates, in `us-east-1` by default (details and costs in [terraform/README.md](terraform/README.md)):

- **A VPC** (`10.0.0.0/16`) with **two public subnets** in two availability zones, an internet
  gateway and a public route table. No NAT gateway: nodes get public IPs, which saves its hourly charge.
- **An EKS cluster** with its IAM role, standard support only (never the $0.60/h extended-support
  rate), and **a managed node group** of 2 × `t3.small` on-demand nodes.
- **Two ECR repositories** (`devoker-backend`, `devoker-frontend`) from one `for_each` block, with
  scan-on-push, immutable tags and a lifecycle policy keeping the last 10 images.
- **An S3 bucket** with a random suffix, versioning, SSE-S3 encryption and all public access blocked.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # optional
terraform init && terraform fmt -check && terraform validate
terraform plan -out tfplan && terraform apply tfplan     # ~15 minutes, mostly EKS
$(terraform output -raw kubeconfig_command) && kubectl get nodes
terraform destroy                                        # ~10 minutes
```

**Cost.** About $0.15/hour while the cluster is up (EKS control plane $0.10, two t3.small ~$0.04,
two public IPv4 addresses $0.01). Left running it's roughly $110–150/month, so it's created for
the screenshots and destroyed the same session. Every resource is tagged `Project=devoker, ManagedBy=terraform`.
`force_delete` and `force_destroy` let `destroy` succeed even when the repositories and bucket hold
data, which is right for a lab and wrong for production. State is local; the bucket is meant for
remote state with locking, which a team would need.

## Troubleshooting

Five incidents from the first, Go version of the backend, each with real output and the same six parts: symptom, investigation, root cause,
fix, verify, prevent. See [troubleshooting/](troubleshooting/README.md).

| # | Symptom | Root cause |
|---|---|---|
| 1 | Backend `CreateContainerConfigError` | `runAsNonRoot` can't verify a named user (happened for real) |
| 2 | "room not found" at random | 2 backend replicas split the in-memory state (solved for good by Postgres) |
| 3 | Frontend crash-loops, then requests hang | nginx resolves the backend name once, at start |
| 4 | Every API call returns 502 | Bad readiness path, made a full outage by `Recreate` (rolling updates now) |
| 5 | HPA `<unknown>` | No CPU request on the frontend |

## Screenshots

**Demo video:** [demo/devoker-demo.mp4](demo/devoker-demo.mp4) (2 min 25 s). A real run from commit to live: the title changes to "Devoker v2", the pipeline goes green, images reach GHCR, the bot commits the new tag, Argo CD syncs, the pods roll, and the change is live.

All in [`screenshots/`](screenshots/), grouped by rubric module.

| Module | Screenshot |
|---|---|
| M1 App | Two players mid-hand: [host](screenshots/02-app-player1-host.png), [second player](screenshots/02-app-player2.png) |
| M2 Tests | [`pytest -v`, 34 passed](screenshots/03-pytest.png) |
| M3 Repo | [Commit history](screenshots/04-commit-history.png) |
| M4 Docker | [`docker compose up --build` and `ps` with 3 services](screenshots/01-docker-compose-up.png) |
| M5 CI/CD | [Green pipeline run](screenshots/05-pipeline-green.png) ([run on GitHub](https://github.com/OKpoorav/Devoker/actions/runs/38800535473)), GHCR with SHA tags: [backend](screenshots/07-ghcr-backend.png), [frontend](screenshots/07-ghcr-frontend.png) |
| M6 Trivy | Image gate in CI, 0 HIGH/CRITICAL: [backend](screenshots/06-trivy-backend.png), [frontend](screenshots/06-trivy-frontend.png) |
| M7 Terraform | [`terraform plan`](screenshots/10-terraform-plan.png), [apply complete + `kubectl get nodes`](screenshots/14-terraform-apply-and-nodes.png), console: [EKS cluster](screenshots/15-aws-eks-cluster.png), [node group nodes](screenshots/16-aws-eks-nodegroup.png), [VPC](screenshots/17-aws-vpc.png), [2 public subnets](screenshots/18-aws-subnets.png), [`terraform destroy`](screenshots/20-terraform-destroy.png) |
| M8 Kubernetes + Helm | [`kubectl get pods`](screenshots/08-kubectl-get-pods.png), [`kubectl get svc,ingress`](screenshots/08-kubectl-get-svc.png), [`helm upgrade --install` + `helm list`](screenshots/09-helm-list.png), [app through the Ingress](screenshots/08-app-via-ingress.png) |
| M9 Monitoring | [`/metrics`](screenshots/11-metrics-endpoint.png), [Prometheus targets UP](screenshots/12-prometheus-targets.png), [Grafana Devoker dashboard](screenshots/13-grafana-dashboard.png) |
| GitOps | [Argo CD app tree, Synced / Healthy](screenshots/19-argocd-synced.png) |

Notes on how they were taken:
- **AWS ran in `ap-southeast-2` (Sydney).** The lab account is on the AWS Free Plan, which only allows
  that region, so Terraform ran from AWS CloudShell with `aws_region = "ap-southeast-2"` in `terraform.tfvars`.
- **`helm list` shows the release in `devoker-staging`.** In the `devoker` namespace Argo CD renders
  the chart itself (`helm template`), so no Helm release is recorded there. The same chart was
  installed with `helm upgrade --install` into `devoker-staging` to show the Helm workflow.
- Terminal screenshots are the real output of each command, rendered in a terminal-style page.

## Lessons learned

1. **The app's state model decides the deployment.** In-memory state forced 1 replica and `Recreate`.
   Moving rooms into Postgres, with a row lock around each change, made the backend stateless: 2
   replicas, rolling updates, and no game lost on deploy.
2. **Security scans find real problems.** The first SAST run found a predictable card shuffle; the
   dependency scan found an outdated toolchain; Trivy found a writable filesystem.
3. **Accepting a finding is fine if it's written down.** `.trivyignore` says why, and when to revisit.
4. **Docker passing isn't Kubernetes passing.** The `runAsNonRoot` failure only shows up in a pod.
5. **Manual changes fight automation.** A `kubectl scale` took field ownership and blocked a Helm 4
   upgrade. GitOps with `selfHeal` removes that whole class of problem.
6. **nginx resolves upstreams once.** A recreated Service leaves old pods pointing at a dead IP.
7. **A migration needs its own commit.** Alembic under a Postgres advisory lock rolled back silently
   until `env.py` committed the lock's connection; the error showed up later as `relation "rooms" does not exist`.

## API

Interactive docs at `/docs` (FastAPI's Swagger UI). Errors are JSON `{"error": "..."}`.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | | `{"status":"UP"}` (liveness) |
| GET | `/ready` | | `{"status":"READY"}` once the database answers (readiness) |
| GET | `/metrics` | | Prometheus metrics |
| POST | `/api/rooms` | `{"name"}` | 201 `{"room","token"}`; the creator is the host |
| POST | `/api/rooms/{code}/join` | `{"name"}` | 201 `{"room","token"}`; waits for the host |
| GET | `/api/rooms/{code}?token=` | | the table as this player may see it |
| GET | `/api/rooms/{code}/hands?token=` | | the last 20 finished hands, from the `hands` table |
| POST | `/api/rooms/{code}/start` | `{"token"}` | 204 |
| POST | `/api/rooms/{code}/action` | `{"token","action":"fold\|check\|call\|raise","amount"}` | 204 |
| POST | `/api/rooms/{code}/rebuy` | `{"token"}` | 204 |
| PUT | `/api/rooms/{code}/requests/{id}` | `{"token","approve"}` | 204 (host only) |
| DELETE | `/api/rooms/{code}/seat` | `{"token"}` | 204; cancels a pending join or leaves between hands |
| DELETE | `/api/rooms/{code}` | `{"token"}` | 204; the host closes the table |
| GET | `/api/rooms/{code}/events?token=` | | SSE stream of the table as JSON |

The token is a secret per player. Only the owner's stream includes their hole cards. Joining sends
the host a request, and the player's stream reports `pending` until the host approves (`seated`) or
declines (`rejected`). A busted player can buy back in between hands; the host's own rebuy is
immediate, everyone else's needs approval. When the host closes the table, every stream gets
`{"status":"closed"}`. There is no turn timer, so a player who closes their tab mid-hand stalls the
table until they return.
