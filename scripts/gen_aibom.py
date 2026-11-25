#!/usr/bin/env python3
"""Generate AIBOM.md: every third-party thing this pipeline pulls at build/deploy time.

Sources (all read from the repo, nothing hand-copied):
  * GitHub Actions   : `uses:` lines in .github/workflows/*.yml (must be pinned to a 40-char SHA)
  * Python packages  : EVERY requirements*.txt tracked in the repo, wherever it lives; each line must be
                       name==version with --hash=, and no -r/-e/URL/index options
  * Other manifests  : pyproject.toml, setup.py, Pipfile, package.json, Dockerfile, go.mod ... are listed with a
                       sha256 so any new or changed one shows up as an AIBOM diff
  * Binaries         : sha256 checks in workflows, plus .github/aibom-binaries.json for version/source
  * In-tree actions  : compiled files under .github/actions/*/dist, with sha256 and whether any workflow uses them
  * Agent (optional) : .github/aibom-agent.json for model, action and settings pins, when step 5 lands

Usage:  python scripts/gen_aibom.py           # rewrite AIBOM.md
        python scripts/gen_aibom.py --check   # exit 1 if AIBOM.md is stale or anything is unpinned
Stdlib only, deterministic output (no timestamps) so the check is a plain diff.
"""
import glob, hashlib, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "AIBOM.md"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
problems = []


def rel(p):
    return str(Path(p).relative_to(ROOT))


def actions():
    rows = {}
    for f in sorted(glob.glob(str(ROOT / ".github/workflows/*.y*ml"))):
        for line in open(f):
            m = re.match(r"\s*-?\s*uses:\s*([^\s#]+)\s*(?:#\s*(\S+))?", line)
            if not m:
                continue
            ref, ver = m.group(1), m.group(2) or ""
            if ref.startswith("./"):
                continue  # local action, reviewed in-tree
            name, _, pin = ref.partition("@")
            if not SHA40.match(pin):
                problems.append(f"{rel(f)}: action {ref} is not pinned to a 40-char SHA")
            rows.setdefault((name, pin, ver), set()).add(Path(f).name)
    return [(n, p, v, ", ".join(sorted(w))) for (n, p, v), w in sorted(rows.items())]


def tracked_files():
    """All files in the repo, wherever they live (git ls-files, falling back to a walk that skips .git)."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
        return sorted(ROOT / l for l in out.splitlines() if l)
    except Exception:
        return sorted(p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.relative_to(ROOT).parts)


REQ_RE = re.compile(r"(?:^|/)requirements[^/]*\.txt$")
OTHER_RE = re.compile(
    r"(?:^|/)(pyproject\.toml|setup\.py|setup\.cfg|Pipfile(\.lock)?|poetry\.lock|package(-lock)?\.json|"
    r"yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|go\.(mod|sum)|Gemfile(\.lock)?|Cargo\.(toml|lock)|"
    r"Dockerfile[^/]*|\.tool-versions)$")


def packages():
    rows = []
    for f in tracked_files():
        r = rel(f)
        if not REQ_RE.search(r):
            continue
        text = re.sub(r"\\\n", " ", open(f).read())
        for raw in text.splitlines():
            line = raw.split(" #")[0].strip() if not raw.lstrip().startswith("#") else ""
            if not line:
                continue
            if line.startswith("-") and not line.startswith("--hash"):
                problems.append(f"{r}: option/indirection not allowed in a requirements file: {line[:60]}")
                continue
            m = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;]+)", line)
            if not m:
                problems.append(f"{r}: not pinned with == : {line[:60]}")
                continue
            hashes = len(re.findall(r"--hash=sha256:[0-9a-f]{64}", line))
            if hashes == 0:
                problems.append(f"{r}: {m.group(1)}=={m.group(2)} has no --hash")
            rows.append((m.group(1).lower(), m.group(2), hashes, r))
    return sorted(rows)


INSTALL_FILES = re.compile(r"(?:^|/)(buildspec[^/]*\.ya?ml|Dockerfile[^/]*|[^/]+\.sh|\.github/(?:workflows|actions)/.+\.ya?ml)$")
PIP_RE = re.compile(r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b")
NPM_RE = re.compile(r"\b(?:npm\s+(?:i|install|ci)|bun\s+(?:i|install|add)|yarn\s+(?:install|add)|pnpm\s+(?:i|install|add))\b")
PIPE_RE = re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")


def install_commands():
    """Install commands typed directly into CI/build files are a dependency source too."""
    for f in tracked_files():
        r = rel(f)
        if not INSTALL_FILES.search(r):
            continue
        for n, raw in enumerate(open(f, errors="replace"), 1):
            line = raw.strip()
            if line.startswith("#") or "pip install pip-tools" in line:
                continue
            if PIP_RE.search(line) and "--require-hashes" not in line:
                problems.append(f"{r}:{n}: pip install without --require-hashes: {line[:70]}")
            if NPM_RE.search(line) and "--ignore-scripts" not in line:
                problems.append(f"{r}:{n}: npm/bun install without --ignore-scripts: {line[:70]}")
            if PIPE_RE.search(line):
                problems.append(f"{r}:{n}: piping a download into a shell: {line[:70]}")


def other_manifests():
    return [(rel(f), hashlib.sha256(open(f, "rb").read()).hexdigest()) for f in tracked_files() if OTHER_RE.search(rel(f))]


def binaries():
    meta_path = ROOT / ".github/aibom-binaries.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    seen = {}
    for f in sorted(glob.glob(str(ROOT / ".github/workflows/*.y*ml"))):
        for m in re.finditer(r'echo "([0-9a-f]{64})\s+(\S+)"\s*\|\s*sha256sum -c', open(f).read()):
            seen.setdefault((m.group(2), m.group(1)), set()).add(Path(f).name)
    rows = []
    for (name, sha), wf in sorted(seen.items()):
        info = meta.get(name, {})
        if not info:
            problems.append(f"binary {name}: add it to .github/aibom-binaries.json (version, source)")
        rows.append((name, info.get("version", "?"), info.get("source", "?"), sha, ", ".join(sorted(wf))))
    for name in meta:
        if name not in {r[0] for r in rows}:
            problems.append(f"binary {name} is in aibom-binaries.json but no workflow verifies its sha256")
    return rows


def bundled():
    """Compiled bundles checked into the repo: hash them so any change to dist/ shows up as an AIBOM diff."""
    wf = "".join(open(f).read() for f in glob.glob(str(ROOT / ".github/workflows/*.y*ml")))
    rows = []
    for d in sorted(glob.glob(str(ROOT / ".github/actions/*/"))):
        name = Path(d).name
        used = f"./.github/actions/{name}" in wf
        for f in sorted(glob.glob(d + "/dist/**/*", recursive=True)):
            if Path(f).is_file():
                data = open(f, "rb").read()
                rows.append((name, rel(f), len(data), hashlib.sha256(data).hexdigest(), "yes" if used else "no"))
    return rows


def agent():
    p = ROOT / ".github/aibom-agent.json"
    return json.loads(p.read_text()) if p.exists() else None


def render():
    install_commands()
    o = ["# AIBOM: third-party build and deploy dependencies", "",
         "Generated by `scripts/gen_aibom.py`. Do not edit; CI fails if it is stale or anything is unpinned.", ""]
    o += ["## GitHub Actions (pinned by commit SHA)", "", "| Action | Commit | Version | Used in |", "|---|---|---|---|"]
    o += [f"| {n} | `{p}` | {v} | {w} |" for n, p, v, w in actions()]
    o += ["", "## Binaries (sha256-verified in the workflow)", "", "| Binary | Version | Source | sha256 | Used in |", "|---|---|---|---|---|"]
    o += [f"| {n} | {v} | {s} | `{h}` | {w} |" for n, v, s, h, w in binaries()]
    pk = packages()
    o += ["", f"## Python packages ({len(pk)}, all installed with `--require-hashes`)", "", "| Package | Version | Hashes | File |", "|---|---|---|---|"]
    o += [f"| {n} | {v} | {h} | {f} |" for n, v, h, f in pk]
    om = other_manifests()
    o += ["", "## Other dependency manifests in the repo (review on every change)", ""]
    if om:
        o += ["| File | sha256 |", "|---|---|"] + [f"| {p} | `{h}` |" for p, h in om]
    else:
        o += ["- None."]
    bd = bundled()
    o += ["", "## Compiled bundles in the repo (review source vs dist on every change)", ""]
    if bd:
        o += ["| Action | File | Bytes | sha256 | Referenced by a workflow |", "|---|---|---|---|---|"]
        o += [f"| {n} | {f} | {b} | `{h}` | {u} |" for n, f, b, h, u in bd]
    else:
        o += ["- None."]
    o += ["", "## Runtime-provided (not shipped by us)", "",
          "- boto3 inside the Lambda runtime (python3.12); the registrar imports only the standard library, boto3 and local modules."]
    a = agent()
    o += ["", "## Agent (Claude Code)", ""]
    if a:
        o += [f"- {k}: {v}" for k, v in sorted(a.items())]
    else:
        o += ["- Not deployed yet. When it is, record model, action SHA, settings file and allowed domains in `.github/aibom-agent.json`."]
    return "\n".join(o) + "\n"


def main():
    text = render()
    if "--check" in sys.argv:
        bad = list(problems)
        if not OUT.exists() or OUT.read_text() != text:
            bad.append("AIBOM.md is stale: run python scripts/gen_aibom.py and commit it")
        for b in bad:
            print("AIBOM:", b)
        sys.exit(1 if bad else 0)
    OUT.write_text(text)
    for p in problems:
        print("AIBOM warning:", p)
    print(f"wrote {rel(OUT)}")


main()
