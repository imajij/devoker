#!/usr/bin/env python3
"""Builds the Devoker explainer video.

Each scene = one 1920x1080 slide (HTML rendered by headless Chrome) + a narration clip (macOS `say`).
ffmpeg turns every pair into a clip sized to its narration, then concatenates them and muxes subtitles.

    python3 build.py            # writes out/devoker-explained.mp4 and out/devoker-explained.srt
"""

import html
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROOF = ROOT.parent / "proof"
SHOTS = PROOF / "shots"
REPO = ROOT.parent / "Devoker"
BUILD = ROOT / "build"
OUT = ROOT / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VOICE = os.environ.get("VOICE", "Samantha")
RATE = os.environ.get("RATE", "172")
PAD = 0.7  # seconds of silence after each narration

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { width: 1920px; height: 1080px; overflow: hidden; background: #0f1a14;
       font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', sans-serif; color: #e8efe9; }
.wrap { position: absolute; inset: 0; padding: 70px 100px; display: flex; flex-direction: column; }
.kicker { color: #e8c35a; font-size: 26px; letter-spacing: 4px; text-transform: uppercase; font-weight: 600; }
h1 { font-size: 64px; margin: 10px 0 34px; font-weight: 750; }
.title h1 { font-size: 120px; margin: 0; }
.title .sub { font-size: 40px; color: #b9cbbf; margin-top: 24px; }
.title .wrap { justify-content: center; }
.body { flex: 1; display: flex; gap: 50px; min-height: 0; }
.col { flex: 1; display: flex; flex-direction: column; gap: 22px; min-width: 0; }
ul { list-style: none; display: flex; flex-direction: column; gap: 22px; }
li { font-size: 34px; line-height: 1.35; padding-left: 40px; position: relative; color: #dbe6de; }
li::before { content: '♠'; position: absolute; left: 0; color: #e8c35a; font-size: 28px; top: 3px; }
li b { color: #fff; }
.term { background: #0a0f0c; border: 1px solid #2c4134; border-radius: 16px; padding: 26px 30px; flex: 1;
        font-family: 'SF Mono', Menlo, monospace; font-size: 21px; line-height: 1.45; color: #cfe3d4;
        white-space: pre; overflow: hidden; box-shadow: 0 20px 60px rgba(0,0,0,.45); }
.term .bar { display: flex; gap: 10px; margin-bottom: 16px; }
.term .bar i { width: 14px; height: 14px; border-radius: 50%; display: block; }
.term .cmd { color: #e8c35a; }
.term .ok { color: #7fd99a; }
.shot { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; gap: 30px; }
.shot img { max-width: 100%; max-height: 100%; border-radius: 14px; border: 1px solid #2c4134;
            box-shadow: 0 20px 60px rgba(0,0,0,.5); object-fit: contain; min-width: 0; }
.shot.two img { max-width: 49%; }
.cap { font-size: 26px; color: #9fb5a6; margin-top: 18px; text-align: center; }
.flow { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-top: 10px; }
.node { background: #1b2c22; border: 2px solid #35553f; border-radius: 14px; padding: 18px 24px; font-size: 28px; font-weight: 600; }
.node.gold { border-color: #e8c35a; color: #f5dc95; }
.arrow { color: #e8c35a; font-size: 36px; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; }
.card { background: #16251c; border: 1px solid #2c4134; border-radius: 16px; padding: 24px 26px; }
.card h3 { font-size: 30px; color: #f5dc95; margin-bottom: 8px; }
.card p { font-size: 24px; color: #c3d3c8; line-height: 1.35; }
.foot { position: absolute; bottom: 30px; right: 100px; color: #5f7a68; font-size: 22px; }
"""


def page(inner: str, cls: str = "", step: str = "") -> str:
    foot = f'<div class="foot">Devoker · {html.escape(step)}</div>' if step else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>" \
           f"<body class='{cls}'><div class='wrap'>{inner}</div>{foot}</body></html>"


def head(kicker: str, title: str) -> str:
    return f"<div class='kicker'>{html.escape(kicker)}</div><h1>{html.escape(title)}</h1>"


def bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def term(text: str, max_lines: int = 26) -> str:
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", l.rstrip("\r")) for l in text.strip("\n").splitlines()]
    lines = [l[:118] for l in lines][-max_lines:]
    out = []
    for l in lines:
        e = html.escape(l)
        if l.startswith("$ "):
            e = f"<span class='cmd'>{e}</span>"
        elif re.search(r"\b(passed|Running|Success|Synced|Healthy|ok:|deployed|UP)\b", l):
            e = f"<span class='ok'>{e}</span>"
        out.append(e)
    dots = "".join(f"<i style='background:{c}'></i>" for c in ("#ff5f57", "#febc2e", "#28c840"))
    return f"<div class='term'><div class='bar'>{dots}</div>{chr(10).join(out)}</div>"


def img(*paths: Path, cap: str = "") -> str:
    cls = "shot two" if len(paths) > 1 else "shot"
    tags = "".join(f"<img src='{p.as_uri()}'>" for p in paths)
    c = f"<div class='cap'>{html.escape(cap)}</div>" if cap else ""
    return f"<div class='{cls}'>{tags}</div>{c}"


def proof(name: str) -> str:
    p = PROOF / name
    return p.read_text(errors="replace") if p.exists() else f"(missing {name})"


def scenes() -> list[dict]:
    S = []
    add = lambda step, body, say, cls="": S.append({"html": page(body, cls, step), "say": say})

    add("", "<div class='kicker'>Project walkthrough</div><h1>Devoker</h1>"
            "<div class='sub'>Texas Hold'em with friends, shipped by a full DevOps pipeline:<br>"
            "FastAPI · PostgreSQL · React · Docker · Kubernetes · Helm · GitHub Actions · Argo CD · Prometheus · Terraform</div>",
        "Welcome. This is Devoker, a multiplayer Texas Hold'em game for playing with friends. "
        "The game itself is small, but it's delivered the way a production service would be: "
        "containerised, deployed to Kubernetes with Helm, scanned for security issues in CI, "
        "rolled out by Argo CD through GitOps, monitored with Prometheus and Grafana, "
        "and backed by Terraform for the AWS side. In the next few minutes I'll walk through what it does, "
        "how it's built, and show every layer actually running on this machine.", "title")

    add("1 · The app", head("1 · The app", "Host a table, friends ask to join") +
        img(SHOTS / "01-lobby.png", SHOTS / "02-join-request.png", cap="Lobby, and the host's view of a pending join request"),
        "Let's start with the product. A player enters a name and hosts a table, which gives them a five-letter room code. "
        "A friend enters the same code and asks to join. The host sees the request appear instantly, and approves or declines it. "
        "Nobody sits at your table without your permission.")

    add("1 · The app", head("1 · The app", "Every player sees only their own cards") +
        img(SHOTS / "03-alice-preflop.png", SHOTS / "03-bob-preflop.png", cap="Same hand, two browsers: Alice (host) and Bob"),
        "Here's a hand in progress from two different browsers. On the left is Alice, the host; on the right is Bob. "
        "Blinds have been posted, the pot is thirty, and each player can see only their own hole cards. "
        "The server builds a separate view of the table for each player's secret token, so cards never leak to the wrong browser. "
        "Alice has the action, with fold, call, raise and all-in buttons.")

    add("1 · The app", head("1 · The app", "Showdown, settled by the server") +
        img(SHOTS / "05-alice-showdown.png", cap="Bob wins 40 with One Pair; chip counts update for both players live"),
        "After the flop, turn and river, the hand goes to showdown. The server evaluates both hands, Bob wins forty chips with one pair, "
        "and the result is recorded in PostgreSQL as hand history. "
        "Both screens update live through Server-Sent Events, without any page refresh.")

    add("2 · Architecture", head("2 · Architecture", "How the pieces fit together") +
        "<div class='flow'><div class='node'>Browser</div><div class='arrow'>→</div><div class='node gold'>Ingress<br><small>devoker.localhost</small></div>"
        "<div class='arrow'>→</div><div class='node'>/ → React on nginx<br><small>2–5 pods, HPA</small></div>"
        "<div class='arrow'>+</div><div class='node'>/api → FastAPI<br><small>2 replicas</small></div><div class='arrow'>→</div>"
        "<div class='node gold'>PostgreSQL 16<br><small>PVC 1 Gi</small></div></div>"
        "<div style='height:40px'></div><div class='flow'><div class='node'>git push</div><div class='arrow'>→</div>"
        "<div class='node'>GitHub Actions<br><small>test · scan · build</small></div><div class='arrow'>→</div><div class='node'>GHCR</div>"
        "<div class='arrow'>→</div><div class='node'>bot bumps tag in Helm values</div><div class='arrow'>→</div><div class='node gold'>Argo CD syncs</div></div>"
        "<div style='height:40px'></div><div class='flow'><div class='node'>Prometheus scrapes /metrics</div><div class='arrow'>→</div>"
        "<div class='node'>Grafana dashboard + alerts</div><div style='width:60px'></div><div class='node'>Terraform: VPC · EKS · ECR · S3</div></div>",
        "Here's the architecture. A browser hits the Kubernetes Ingress. Slash goes to the React frontend served by nginx, which autoscales between two and five pods. "
        "Slash A P I goes to the FastAPI backend, running two replicas, and all state lives in PostgreSQL. "
        "Along the bottom is the delivery path: a git push triggers GitHub Actions, which tests, scans and builds the images, pushes them to the GitHub container registry, "
        "and then commits the new image tag back into the Helm chart. Argo CD, running inside the cluster, notices that commit and deploys it. "
        "Prometheus scrapes the backend's metrics, Grafana visualises them, and Terraform describes the equivalent AWS infrastructure.")

    add("3 · Backend", head("3 · Backend design", "Stateless replicas, state in Postgres") +
        "<div class='body'><div class='col'>" + bullets([
            "<b>Every room is one Postgres row</b> holding the table as JSON, plus a <b>version</b> counter",
            "Each action runs under <b>SELECT … FOR UPDATE</b>, so two replicas queue instead of overwriting",
            "Live updates: each SSE stream <b>polls the version</b> every 0.5 s and pushes a new view when it moves",
            "<b>Alembic migrations</b> run at startup behind an advisory lock",
            "<b>/health</b>, <b>/ready</b> (runs SELECT 1) and <b>/metrics</b> for Kubernetes and Prometheus",
        ]) + "</div><div class='col'>" + term(
            "$ backend/app/rooms.py\n\n@contextmanager\ndef locked(db, code):\n    \"\"\"SELECT ... FOR UPDATE, save on exit.\"\"\"\n"
            "    row, room = load(db, code, lock=True)\n    yield row, room\n    row.state = room.to_dict()\n    row.version += 1\n    db.commit()\n\n"
            "$ backend/app/main.py\n\n@app.post(\"/api/rooms/{code}/action\")\ndef action(code, body, db):\n    with rooms.locked(db, code) as (row, room):\n"
            "        room.table.act(room.seated(body.token),\n                       body.action, body.amount)") + "</div></div>",
        "The key backend decision is that the API is stateless. The first version kept games in memory, which meant only one replica and downtime on every deploy. "
        "Now every room is a single Postgres row containing the table as JSON and a version number. "
        "Every change, like a bet or a fold, happens inside this locked helper, which takes a row lock with select for update, applies the change, bumps the version and commits. "
        "So two players acting at the same moment on different replicas simply queue on the lock. "
        "Each player's event stream watches the version column and pushes a fresh view whenever it changes, no matter which replica made the change.")

    add("4 · Tests", head("4 · Tests", "36 pytest tests against a throwaway database") +
        term("$ cd backend && pytest -v\n" + proof("02-pytest.txt"), 24),
        "Next, tests. The project shipped with thirty-four pytest tests covering the poker rules, hand rankings, and every API endpoint, "
        "and I added two more for a bug I'll show you later. "
        "The test fixture builds a throwaway SQLite database by running the real Alembic migrations, so the schema under test is exactly what production gets. "
        "Here they are, all thirty-six passing on this machine.")

    add("5 · Docker", head("5 · Docker", "Multi-stage, non-root images and a 3-service Compose stack") +
        "<div class='body'><div class='col' style='flex:.8'>" + bullets([
            "Backend: <b>python:3.12-slim</b>, deps built into a venv, runs as <b>UID 10001</b>",
            "Frontend: Node build stage, served by <b>nginx-unprivileged</b> as UID 101",
            "Compose: <b>postgres</b> with a healthcheck, <b>backend</b> waits for healthy, <b>frontend</b> on :3000",
            "Smoke test plays a full hand through the public API",
        ]) + "</div><div class='col'>" + term("$ docker compose ps\n" + proof("01-compose-ps.txt") +
                                               "\n$ python3 scripts/smoke.py\n" + proof("01-compose-smoke.txt"), 14) + "</div></div>",
        "For containers, both images use multi-stage builds. The backend installs its dependencies in a build stage and copies only the virtual environment into a slim runtime image. "
        "The frontend is compiled with Node, and only the static files ship, in an unprivileged nginx image. Both run as numeric, non-root users, which Kubernetes needs to verify run as non root. "
        "Docker Compose brings up Postgres, the backend and the frontend, and the backend waits until Postgres is healthy. "
        "The smoke script then plays a complete hand through the API, and here it reports success.")

    add("6 · Kubernetes + Helm", head("6 · Kubernetes + Helm", "Deployed to minikube with the Helm chart") +
        term("$ kubectl -n devoker get pods,svc,ingress,hpa\n" + proof("08-kubectl-devoker.txt"), 26),
        "Now Kubernetes. Everything is packaged as one Helm chart: deployments for the backend, frontend and Postgres, cluster IP services, an Ingress, a horizontal pod autoscaler, "
        "persistent volumes, a config map for blinds and starting chips, and a secret for the database password that's created with kubectl and never committed. "
        "Here are the running pods: two backend replicas, two frontend pods managed by the autoscaler, and Postgres. "
        "Every container drops all Linux capabilities and runs as non-root with a read-only root file system. "
        "An init container waits for Postgres, so the backend never crash-loops on a fresh install.")

    add("6 · Kubernetes + Helm", head("6 · Kubernetes + Helm", "The app through the Ingress") +
        img(SHOTS / "08-app-via-ingress.png", cap="http://devoker.localhost:8081 → ingress-nginx → frontend / backend services"),
        "And here's the same game served through the ingress controller at devoker dot localhost. "
        "The Ingress routes slash A P I to the backend and everything else to the frontend, with buffering turned off and a long read timeout so the event streams stay open.")

    add("7 · CI/CD + DevSecOps", head("7 · CI/CD + DevSecOps", "Every push to main runs this pipeline") +
        "<div class='grid'>"
        "<div class='card'><h3>Test</h3><p>pytest (36) and a production Vite build of the frontend</p></div>"
        "<div class='card'><h3>SAST</h3><p>bandit on the backend, fails on HIGH</p></div>"
        "<div class='card'><h3>SCA</h3><p>pip-audit, npm audit, Trivy fs for dependency and Helm/K8s misconfig</p></div>"
        "<div class='card'><h3>Secrets</h3><p>Gitleaks across the full git history</p></div>"
        "<div class='card'><h3>Build + gate</h3><p>multi-arch buildx images, Trivy image scan: no fixable HIGH/CRITICAL allowed</p></div>"
        "<div class='card'><h3>Deliver</h3><p>push to GHCR tagged by commit SHA, bot commits the tag to Helm values</p></div>"
        "</div>",
        "Delivery is handled by GitHub Actions. On every push, the pipeline runs the tests and a production frontend build, "
        "then four kinds of security scanning: bandit for static analysis, pip audit, npm audit and Trivy for vulnerable dependencies and Kubernetes misconfigurations, "
        "and Gitleaks to catch secrets anywhere in the git history. "
        "Only when all of that passes are the images built, for both amd64 and arm64, and scanned again by Trivy. "
        "Any fixable high or critical vulnerability stops the release. Images that pass are pushed to the GitHub container registry, tagged with the commit SHA, "
        "and a bot commits that tag into the Helm values file.")

    add("8 · GitOps", head("8 · GitOps with Argo CD", "Git is the source of truth") +
        img(SHOTS / "09-argocd.png", cap="Argo CD application 'devoker': Synced and Healthy, tracking helm/devoker on main"),
        "That commit is where GitOps takes over. Argo CD runs inside the cluster and watches the Helm chart in the repository. "
        "When the image tag changes, it renders the chart and syncs the cluster to match. "
        "Self-heal is on, so a manual kubectl edit gets reverted, and prune removes anything that git no longer contains. "
        "Here it is on this cluster, synced and healthy, pulling the published images straight from the registry.")

    add("9 · Monitoring", head("9 · Monitoring", "Prometheus scrapes the backend; Grafana shows it") +
        img(SHOTS / "10-prometheus-targets.png", SHOTS / "11-grafana.png", cap="ServiceMonitor targets UP, and the Devoker Grafana dashboard"),
        "For observability, the backend exposes Prometheus metrics: request rates and latencies, plus custom metrics for hands played and open event streams. "
        "The kube Prometheus stack is installed with Helm, and a ServiceMonitor in the Devoker chart tells Prometheus to scrape both backend replicas. "
        "The chart also ships a Grafana dashboard, and two alert rules: one for backend restarts, and one for the frontend autoscaler sitting at its maximum.")

    add("10 · A real bug", head("10 · Found under load", "Event streams were pinning database connections") +
        "<div class='body'><div class='col' style='flex:1.1'>" + img(SHOTS / "11-grafana.png") +
        "<div class='cap'>Burst of 25 games: p95 latency pinned at 1 s, 15 open streams, Ingress returned 503</div></div>"
        "<div class='col'>" + term(proof("12-bug-before-after.txt"), 22) + "</div></div>",
        "Monitoring paid off right away. When I pushed a burst of twenty-five games through the Ingress, about a third of the requests failed with five oh three. "
        "On the dashboard you can see why: p95 latency jumps to one second and fifteen event streams are open. "
        "The cause was the event stream endpoint. It took its database session through FastAPI's dependency injection, and FastAPI keeps that session open until the stream ends. "
        "So every connected player held a pooled connection, idle in transaction. Fifteen streams used up the whole pool, "
        "the readiness check had to wait for a connection, its one second probe timed out, Kubernetes marked both backends unready, and the Ingress had nowhere to send traffic. "
        "The fix is small: the stream checks the player's token with its own short-lived session, just like the polling loop already did. "
        "After the fix, the same burst leaves no connections stuck, readiness answers in ten milliseconds, and twenty-five out of twenty-five games succeed. "
        "I added a regression test that fails on the old code.")

    add("11 · Terraform", head("11 · Terraform", "The AWS side, as code") +
        "<div class='body'><div class='col'>" + bullets([
            "<b>VPC</b> 10.0.0.0/16 with two public subnets in two AZs, no NAT gateway to save cost",
            "<b>EKS</b> cluster plus a managed node group of 2 × t3.small",
            "<b>ECR</b> repos with scan-on-push, immutable tags, lifecycle policy",
            "<b>S3</b> bucket: versioned, encrypted, public access blocked",
        ]) + "</div><div class='col'>" + term("$ terraform init && terraform fmt -check && terraform validate\n" + proof("07-terraform-validate.txt"), 10) +
        "</div></div>",
        "Finally, Terraform describes the cloud side: a VPC with two public subnets, an EKS cluster with a small managed node group, "
        "E C R repositories with scan on push, and an encrypted S3 bucket. The configuration formats cleanly and validates here. "
        "Applying it creates real, billable AWS resources, about fifteen cents an hour, so it's only planned and applied when needed, and destroyed in the same session.")

    add("Wrap-up", head("Wrap-up", "What this project demonstrates") +
        bullets([
            "<b>State decides deployment</b>: moving rooms into Postgres made the backend horizontally scalable",
            "<b>Security gates are real</b>: scans caught a predictable shuffle, old dependencies, a writable filesystem",
            "<b>Load-test what you monitor</b>: the dashboard exposed a connection-pool leak in the event streams, now fixed",
            "<b>GitOps removes drift</b>: self-heal undoes manual changes",
            "Verified locally: <b>36/36 tests</b>, Compose, Helm on minikube, Argo CD synced, Prometheus + Grafana",
        ]),
        "To wrap up. The biggest lesson is that the application's state model decides how it can be deployed: moving rooms into Postgres is what made two replicas and zero-downtime rolling updates possible. "
        "The security scans found real problems, and load testing against the dashboards found one more, which is now fixed and covered by a test. "
        "Everything you've seen here was run on this machine: the tests, Docker Compose, the Helm chart on minikube, Argo CD, and the monitoring stack. Thanks for watching.")
    return S


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    clips, srt, t = [], [], 0.0
    for i, sc in enumerate(scenes(), 1):
        n = f"{i:02}"
        h, png, aiff, mp4 = BUILD / f"{n}.html", BUILD / f"{n}.png", BUILD / f"{n}.aiff", BUILD / f"{n}.mp4"
        h.write_text(sc["html"])
        run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
             "--allow-file-access-from-files", "--window-size=1920,1080", f"--screenshot={png}", h.as_uri()])
        run(["say", "-v", VOICE, "-r", RATE, "-o", str(aiff), sc["say"]])
        d = duration(aiff) + PAD
        run(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(png), "-i", str(aiff),
             "-filter_complex", f"[0:v]scale=1920:1080,format=yuv420p,fade=t=in:st=0:d=0.4,fade=t=out:st={d - 0.4:.2f}:d=0.4[v];"
                                f"[1:a]aresample=48000,apad=pad_dur={PAD}[a]",
             "-map", "[v]", "-map", "[a]", "-t", f"{d:.2f}", "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage",
             "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(mp4)])
        # one subtitle cue per sentence, timed proportionally to its length
        sentences = re.split(r"(?<=[.!?])\s+", sc["say"].strip())
        total = sum(len(s) for s in sentences)
        span, cur = d - PAD, t
        for s in sentences:
            dur = span * len(s) / total
            srt.append(f"{len(srt) + 1}\n{srt_time(cur)} --> {srt_time(cur + dur)}\n{s}\n")
            cur += dur
        t += d
        clips.append(mp4)
        print(f"scene {n}: {d:5.1f}s")
    (BUILD / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips))
    srt_path = OUT / "devoker-explained.srt"
    srt_path.write_text("\n".join(srt))
    joined = BUILD / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(BUILD / "list.txt"), "-c", "copy", str(joined)])
    run(["ffmpeg", "-y", "-i", str(joined), "-i", str(srt_path), "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text",
         "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", str(OUT / "devoker-explained.mp4")])
    print(f"total {t / 60:.1f} min -> {OUT / 'devoker-explained.mp4'}")


if __name__ == "__main__":
    main()
