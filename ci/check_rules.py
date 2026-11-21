#!/usr/bin/env python3
"""Check the rendered manifests against the rules in AGENTS.md.

- Every Application's sync-wave matches the "Sync waves" table, and every
  Application is listed there (and every listed one exists).
- Every Application retries forever (syncPolicy.retry.limit: -1).
- Every pinned MetalLB IP is unique, inside an IPAddressPool, and listed in
  the "IP address allocations" table (and every listed IP is in use).
- Every source pointing at this repo uses {{ .Values.revision }}, not a
  hardcoded HEAD or branch (AGENTS.md 'Manifests are a Helm chart').
- No Secret with data is committed under apps/ (secrets come from
  ExternalSecrets).

Usage: ci/check_rules.py [--rendered DIR]
"""

import argparse
import ipaddress
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import is_this_repo, load_docs, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
errors = []


def error(msg):
    errors.append(msg)
    print(f"::error::{msg}")


def section(md, title):
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    if not m:
        sys.exit(f"AGENTS.md has no '## {title}' section")
    return m.group(1)


def table_rows(text):
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s\-|]+\|$", line):
            yield [c.strip() for c in line.strip("|").split("|")]


def check_waves(apps, md):
    want = {}
    for row in table_rows(section(md, "Sync waves")):
        if re.fullmatch(r"-?\d+", row[0]):
            for name in re.findall(r"`([^`]+)`", row[2]):
                want[name] = row[0]
    have = {}
    for app in apps:
        name = app["metadata"]["name"]
        have[name] = (app["metadata"].get("annotations") or {}).get("argocd.argoproj.io/sync-wave", "0")
        retry = (app["spec"].get("syncPolicy") or {}).get("retry") or {}
        if retry.get("limit") != -1:
            error(f"Application {name}: syncPolicy.retry.limit must be -1 (AGENTS.md 'Sync waves')")
    for name, wave in sorted(have.items()):
        if name not in want:
            error(f"Application {name} (wave {wave}) is missing from the AGENTS.md 'Sync waves' table")
        elif want[name] != wave:
            error(f"Application {name} has sync-wave {wave}, but AGENTS.md 'Sync waves' lists it under {want[name]}")
    for name in sorted(set(want) - set(have)):
        error(f"AGENTS.md 'Sync waves' lists {name}, but there is no such Application")


def pool_ranges(docs):
    for d in docs:
        if d.get("kind") == "IPAddressPool":
            for a in d["spec"].get("addresses", []):
                if "-" in a:
                    lo, hi = (ipaddress.ip_address(x.strip()) for x in a.split("-"))
                else:
                    net = ipaddress.ip_network(a.strip(), strict=False)
                    lo, hi = net[0], net[-1]
                yield lo, hi


def pinned_ips(docs):
    for d in docs:
        md = d.get("metadata") or {}
        where = f"{d.get('kind')} {md.get('namespace', '')}/{md.get('name')}"
        ann = (md.get("annotations") or {}).get("metallb.io/loadBalancerIPs")
        # Gateway API resources carry Service annotations under spec.infrastructure
        infra = ((d.get("spec") or {}).get("infrastructure") or {}).get("annotations") or {}
        ann = ann or infra.get("metallb.io/loadBalancerIPs")
        if ann:
            for ip in str(ann).split(","):
                yield ip.strip(), where
        lb = (d.get("spec") or {}).get("loadBalancerIP") if d.get("kind") == "Service" else None
        if lb:
            yield lb, where


def check_ips(docs, md):
    listed = set()
    for row in table_rows(section(md, "IP address allocations")):
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", row[0]):
            listed.add(row[0])
    pools = list(pool_ranges(docs))
    if not pools:
        error("no MetalLB IPAddressPool found in the rendered manifests")
    seen = {}
    for ip, where in pinned_ips(docs):
        if ip in seen and seen[ip] != where:
            error(f"{ip} is pinned by both {seen[ip]} and {where}")
        seen[ip] = where
        addr = ipaddress.ip_address(ip)
        if pools and not any(lo <= addr <= hi for lo, hi in pools):
            error(f"{where} pins {ip}, which is outside every MetalLB IPAddressPool")
        if ip not in listed:
            error(f"{where} pins {ip}, which is missing from the AGENTS.md 'IP address allocations' table")
    for ip in sorted(listed - set(seen)):
        error(f"AGENTS.md 'IP address allocations' lists {ip}, but nothing pins it")


def check_revisions():
    sentinel = "ci-revision-sentinel"
    out = run(["helm", "template", "root", str(ROOT / "manifests"), "--set", f"revision={sentinel}"])
    for app in load_docs(out):
        spec = app.get("spec") or {}
        for src in spec.get("sources") or [spec.get("source") or {}]:
            if is_this_repo(src.get("repoURL", "")) and src.get("targetRevision") != sentinel:
                error(f"Application {app['metadata']['name']}: a source for this repo hardcodes "
                      f"targetRevision {src.get('targetRevision')}; use {{{{ .Values.revision }}}}")


def check_secrets():
    for f in sorted((ROOT / "apps").rglob("*")):
        if f.suffix not in (".yaml", ".yml", ".json") or not f.is_file():
            continue
        try:
            docs = load_docs(f.read_text())
        except yaml.YAMLError:
            continue  # templated files (e.g. Coder's Terraform templates) aren't manifests
        for d in docs:
            if d.get("kind") == "Secret" and (d.get("data") or d.get("stringData")):
                error(f"{f.relative_to(ROOT)} commits a Secret with data; use an ExternalSecret (AGENTS.md 'ExternalSecrets')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rendered", default="rendered", type=Path)
    args = ap.parse_args()
    md = (ROOT / "AGENTS.md").read_text()
    apps = [d for d in load_docs((args.rendered / "_applications.yaml").read_text()) if d.get("kind") == "Application"]
    docs = []
    for f in sorted(args.rendered.glob("*.yaml")):
        if f.name != "_applications.yaml":
            docs += load_docs(f.read_text())
    check_waves(apps, md)
    check_ips(docs, md)
    check_revisions()
    check_secrets()
    if errors:
        sys.exit(f"{len(errors)} rule violation(s)")
    print(f"ok: {len(apps)} Applications, {len(docs)} resources")


if __name__ == "__main__":
    main()
