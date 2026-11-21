#!/usr/bin/env python3
"""Render every Argo CD Application in this repo to plain manifests.

Does what Argo CD does for the sources this repo uses, without a cluster:
renders the root chart (manifests/), then each child Application's sources:
Helm charts (HTTP or OCI repos, with $values value files), directories in
this repo (recurse/include/exclude), kustomize directories, and directories
in other git repos.

Writes <out>/<application>.yaml, one file per Application, with documents
sorted so two renders diff cleanly. Secret data is redacted, so the output
is safe to post in a public PR comment.

Usage: ci/render.py [--repo-root DIR] [--out DIR]
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

THIS_REPO = "https://github.com/alextio/my-homelab-k8s"
MANIFEST_EXT = (".yaml", ".yml", ".json")


def run(cmd, cwd=None):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{res.stderr}")
    return res.stdout


class Loader(yaml.SafeLoader):
    """SafeLoader without YAML 1.1's '=' value tag, which breaks on a bare `=`."""


Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:value"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_docs(text):
    return [d for d in yaml.load_all(text, Loader=Loader) if isinstance(d, dict)]


def is_this_repo(url):
    return url.rstrip("/").removesuffix(".git") == THIS_REPO


def expand_braces(pattern):
    """'{a,b/*}' -> ['a', 'b/*']: Argo CD globs allow one brace group."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [pattern[: m.start()] + alt + pattern[m.end():] for alt in m.group(1).split(",")]


def glob_match(rel, pattern):
    return any(fnmatch.fnmatch(rel, p) for p in expand_braces(pattern)) if pattern else False


def render_directory(root, directory):
    directory = directory or {}
    files = root.rglob("*") if directory.get("recurse") else root.glob("*")
    docs = []
    for f in sorted(files):
        if not f.is_file() or f.suffix not in MANIFEST_EXT:
            continue
        rel = f.relative_to(root).as_posix()
        if directory.get("include") and not glob_match(rel, directory["include"]):
            continue
        if glob_match(rel, directory.get("exclude")):
            continue
        docs += load_docs(f.read_text())
    return docs


def render_path(root, source):
    if any((root / k).exists() for k in ("kustomization.yaml", "kustomization.yml", "Kustomization")):
        return load_docs(run(["kubectl", "kustomize", str(root)]))
    return render_directory(root, source.get("directory"))


_clones = {}


def clone(url, rev, workdir):
    key = (url, rev)
    if key not in _clones:
        dest = Path(tempfile.mkdtemp(dir=workdir))
        run(["git", "clone", "-q", "--depth", "1", "--branch", rev, url, str(dest)])
        _clones[key] = dest
    return _clones[key]


def render_helm(app, source, refs, namespace):
    helm = source.get("helm") or {}
    url, chart, version = source["repoURL"], source["chart"], source["targetRevision"]
    release = helm.get("releaseName", app)
    if "://" not in url or url.startswith("oci://"):  # Argo CD treats a bare host/path as OCI
        ref = f"oci://{url.removeprefix('oci://').rstrip('/')}/{chart}"
        cmd = ["helm", "template", release, ref, "--version", version]
    else:
        cmd = ["helm", "template", release, chart, "--repo", url, "--version", version]
    cmd += ["--namespace", namespace or "default", "--include-crds"]
    if helm.get("skipCrds"):
        cmd.remove("--include-crds")
    for vf in helm.get("valueFiles", []):
        name, _, rest = vf.partition("/")
        if not name.startswith("$"):
            sys.exit(f"{app}: value file {vf} is relative to the chart; not supported")
        cmd += ["--values", str(refs[name[1:]] / rest)]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        if helm.get("values"):
            tmp.write(helm["values"])
        elif helm.get("valuesObject"):
            yaml.safe_dump(helm["valuesObject"], tmp)
    if helm.get("values") or helm.get("valuesObject"):
        cmd += ["--values", tmp.name]
    for p in helm.get("parameters", []):
        cmd += ["--set-string" if p.get("forceString") else "--set", f"{p['name']}={p['value']}"]
    try:
        return load_docs(run(cmd))
    finally:
        os.unlink(tmp.name)


def redact(doc):
    if doc.get("kind") == "Secret":
        for field in ("data", "stringData"):
            if isinstance(doc.get(field), dict):
                doc[field] = {k: "<redacted>" for k in doc[field]}
    return doc


def sort_key(doc):
    md = doc.get("metadata") or {}
    return (doc.get("apiVersion", ""), doc.get("kind", ""), md.get("namespace") or "", md.get("name") or "")


def render_app(app_doc, repo_root, workdir):
    name = app_doc["metadata"]["name"]
    spec = app_doc["spec"]
    namespace = spec["destination"].get("namespace")
    sources = spec.get("sources") or [spec["source"]]

    def source_root(src):
        if is_this_repo(src["repoURL"]):
            return repo_root
        return clone(src["repoURL"], src["targetRevision"], workdir)

    refs = {s["ref"]: source_root(s) for s in sources if s.get("ref")}
    docs = []
    for src in sources:
        if src.get("chart"):
            docs += render_helm(name, src, refs, namespace)
        elif src.get("path"):
            docs += render_path(source_root(src) / src["path"], src)
    for d in docs:
        md = d.setdefault("metadata", {})
        if namespace and not md.get("namespace") and not d.get("kind", "").startswith("Cluster"):
            md.setdefault("namespace", namespace)  # Argo CD's destination namespace default
    return sorted((redact(d) for d in docs), key=sort_key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=Path(__file__).resolve().parent.parent, type=Path)
    ap.add_argument("--out", default="rendered", type=Path)
    args = ap.parse_args()
    repo_root = args.repo_root.resolve()
    args.out.mkdir(parents=True, exist_ok=True)

    apps = load_docs(run(["helm", "template", "root", str(repo_root / "manifests")]))
    (args.out / "_applications.yaml").write_text(yaml.safe_dump_all(sorted(apps, key=sort_key), sort_keys=True))
    with tempfile.TemporaryDirectory() as workdir:
        for app in apps:
            if app.get("kind") != "Application":
                continue
            name = app["metadata"]["name"]
            print(f"rendering {name}", file=sys.stderr)
            docs = render_app(app, repo_root, workdir)
            (args.out / f"{name}.yaml").write_text(yaml.safe_dump_all(docs, sort_keys=True))


if __name__ == "__main__":
    main()
