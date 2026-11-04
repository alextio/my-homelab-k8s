# my-homelab-k8s

GitOps repo for the homelab Kubernetes cluster, built on [k3s](https://k3s.io/)
and managed by [Argo CD](https://argo-cd.readthedocs.io/). Argo CD watches this
repo and reconciles everything under `manifests/` (one `Application` per
service) and `apps/` (the config each `Application` consumes).

Each service is added in small, self-contained steps, so the repo doubles as a
record of what was learned along the way.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Core Stack](#core-stack)
- [Deployed Applications](#deployed-applications)
- [Repository Structure](#repository-structure)
- [Sync Waves](#sync-waves)
- [Secrets Management](#secrets-management)
- [Bootstrap](#bootstrap)
- [Day-to-day](#day-to-day)
- [Troubleshooting](#troubleshooting)
- [Tear down](#tear-down)

---

## Architecture Overview

```
   uth-ansible-playbooks (k3s-argocd)          Bitwarden Secrets Manager
   installs k3s + Argo CD, once                (secret values, never in Git)
                 │                                          │
                 ▼                                          │
  ┌──────────────────────────────────────────────────┐      │
  │                   k3s Cluster                    │      │
  │                                                  │      │
  │   Argo CD           (GitOps controller)          │      │
  │   MetalLB           (LoadBalancer IPs, L2/ARP)   │      │
  │   Traefik           (Gateway API ingress, TLS)   │      │
  │   cert-manager      (in-cluster homelab CA)      │      │
  │   External Secrets  (Bitwarden → Secrets)  ◄─────┼──────┘
  │                                                  │
  └──────────────────────────────────────────────────┘
                 ▲                         │
                 │ pulls                   │ 192.168.6.200  *.lan (HTTPS)
                 │                         │ 192.168.6.201  DNS (AdGuard)
        GitHub: alextio/my-homelab-k8s     ▼
        (this repo, source of truth)      LAN
```

**Bootstrapping is out of scope here.** The k3s cluster and the Argo CD install
are handled by the `k3s-argocd` playbook in
[uth-ansible-playbooks](https://codeberg.org/alextio/uth-ansible-playbooks).
This repo starts at the point where Argo CD is running in the `argocd`
namespace: one `kubectl apply -f app-of-apps.yaml` hands everything else over
to Git.

---

## Core Stack

| Component | Purpose | Namespace |
|-----------|---------|-----------|
| [Argo CD](https://argo-cd.readthedocs.io/) | GitOps: syncs this repo to the cluster (installed by the Ansible playbook) | `argocd` |
| [Gateway API CRDs](https://gateway-api.sigs.k8s.io/) | `Gateway` / `HTTPRoute` resource types | cluster-scoped |
| [MetalLB](https://metallb.universe.tf/) | LoadBalancer IP allocation (L2/ARP mode) from `default-pool` | `metallb-system` |
| [Traefik](https://traefik.io/) | Ingress via a shared Gateway API `Gateway`, TLS termination for `*.lan` | `traefik` |
| [cert-manager](https://cert-manager.io/) | Self-signed homelab root CA and the `homelab-ca` ClusterIssuer | `cert-manager` |
| [External Secrets Operator](https://external-secrets.io/) | Renders Kubernetes Secrets from Bitwarden Secrets Manager | `external-secrets` |

> **Why these choices?**
> - **Gateway API over `Ingress`**: Traefik serves one shared `Gateway`
>   (`apps/traefik/gateway.yaml`) with a `*.lan` HTTPS listener. Each app
>   attaches its own `HTTPRoute`, so routing config lives with the app rather
>   than with the ingress controller.
> - **A private CA instead of Let's Encrypt**: `*.lan` isn't a public domain,
>   so ACME can't issue for it. cert-manager bootstraps a self-signed root CA
>   and issues a single `*.lan` wildcard (`apps/traefik/certificate-wildcard.yaml`)
>   for the Gateway. The same CA also signs the bitwarden-sdk-server cert that
>   ESO talks to.
> - **External Secrets + Bitwarden over Sealed Secrets**: secret values live
>   in Bitwarden Secrets Manager, not in Git, encrypted or otherwise. Git only
>   holds `ExternalSecret` objects that point at a Bitwarden secret UUID.
>   Rotating a value means changing it in Bitwarden, not re-sealing and
>   committing.
> - **MetalLB in L2 mode**: no BGP-capable router needed. All traffic for an
>   IP flows through one node at a time, with automatic failover. That's
>   enough for a homelab. See `apps/metallb/l2-advertisement.yaml`.

### IP allocations

MetalLB's `default-pool` covers `192.168.6.200`–`192.168.6.210`, outside the
router's DHCP range. The source of truth is the table in
[AGENTS.md](AGENTS.md#ip-address-allocations).

| IP | Service |
|----|---------|
| `192.168.6.200` | Traefik: HTTP/HTTPS for every `*.lan` host |
| `192.168.6.201` | AdGuard Home: DNS on 53 TCP/UDP (bypasses Traefik) |
| `192.168.6.202`–`210` | free |

---

## Deployed Applications

| Application | Purpose | Namespace | URL |
|-------------|---------|-----------|-----|
| [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics | `monitoring` | `grafana.lan`, `prometheus.lan` |
| [AdGuard Home](https://adguard.com/adguard-home/overview.html) | Network-wide DNS filtering | `adguard-home` | `adguard.lan` (DNS on `192.168.6.201`) |
| [Dex](https://dexidp.io/) | OIDC identity provider | `dex` | `dex.lan` |
| [agentgateway](https://agentgateway.dev/) | AI/LLM gateway (Claude and OpenRouter backends) | `agentgateway-system` | n/a |

The Traefik dashboard is at `traefik.lan/dashboard/`.

`apps/` also holds staged config for `homepage`, `immich` and `langfuse`. They
have no `Application` in `manifests/` yet, so Argo CD doesn't deploy them.

---

## Repository Structure

```
03-argocd-kubernetes/
│
├── app-of-apps.yaml              # Root Application: points Argo CD at manifests/.
│                                 # The only thing applied by hand.
│
├── manifests/                    # One Argo CD Application per service
│   ├── gateway-api-crds.yaml
│   ├── cert-manager-crds.yaml
│   ├── cert-manager.yaml
│   ├── metallb.yaml
│   ├── external-secrets.yaml
│   ├── traefik.yaml
│   ├── kube-prometheus-stack.yaml
│   ├── adguard-home.yaml
│   ├── dex.yaml
│   └── agentgateway.yaml
│
├── apps/                         # Config each Application consumes
│   ├── cert-manager/             # values.yaml, self-signed + CA ClusterIssuers, root CA
│   ├── cert-manager-crds/        # Kustomization pinning the CRD version
│   ├── metallb/                  # values.yaml, IPAddressPool, L2Advertisement
│   ├── external-secrets/         # values.yaml, Bitwarden ClusterSecretStore, sdk-server cert
│   ├── traefik/                  # values.yaml, shared Gateway, *.lan wildcard cert, dashboard
│   ├── kube-prometheus-stack/    # values.yaml, HTTPRoutes, Grafana dashboards
│   ├── adguard-home/             # values.yaml, ConfigMap, ExternalSecret
│   ├── dex/                      # values.yaml, HTTPRoute, ExternalSecret
│   ├── agentgateway/             # Gateway, LLM backends, OTel collector
│   └── <service>/                # Add new services here
│
├── .journey/                     # Write-ups of each service as it was set up
├── .claude/skills/               # Agent skills (e.g. bws-upsert-secret)
├── AGENTS.md                     # Conventions: sync waves, IPs, ExternalSecrets
└── README                        # You are here
```

Most Applications use the same three-source pattern:

1. The upstream Helm chart, with `valueFiles: $values/apps/<service>/values.yaml`.
2. This repo as `ref: values`, so the chart can read `values.yaml` from it.
3. This repo at `path: apps/<service>` (excluding `values.yaml`) for the
   extra resources the chart doesn't render, such as Certificates,
   HTTPRoutes and ExternalSecrets.

---

## Sync Waves

Children sync in order of their `argocd.argoproj.io/sync-wave` annotation.
Argo CD waits for every Application in a wave to be Healthy before starting
the next. The canonical table lives in [AGENTS.md](AGENTS.md#sync-waves).

| Wave | What | Applications |
|------|------|--------------|
| -3 | CRDs only | `gateway-api-crds`, `cert-manager-crds` |
| -2 | Operators / controllers | `cert-manager`, `metallb`, `external-secrets` |
| -1 | Ingress | `traefik` |
| 0 | Workloads (default) | `kube-prometheus-stack`, `agentgateway`, `adguard-home`, `dex` |

Inside an Application, the chart's own resources are wave 0. Resources that
need the chart running first, such as cert-manager's ClusterIssuers or ESO's
ClusterSecretStore, are wave 1.

---

## Secrets Management

No secret values are committed. Secrets live in **Bitwarden Secrets Manager**
(project `k3s-prod`) and are rendered into the cluster by the **External
Secrets Operator**:

```
Bitwarden Secrets Manager
        │  (machine-account access token)
        ▼
bitwarden-sdk-server  ◄── TLS cert from cert-manager's homelab-ca
        │
        ▼
ClusterSecretStore "bitwarden"   (apps/external-secrets/clustersecretstore-bitwarden.yaml)
        │
        ▼
ExternalSecret in apps/<service>/  ──►  Kubernetes Secret
```

The only secret created by hand is the Bitwarden machine-account token, once,
before `external-secrets` syncs:

```sh
kubectl -n external-secrets create secret generic bitwarden-access-token \
  --from-literal=token='<machine-account-access-token>'
```

To add a secret for a service:

1. Create the value in Bitwarden with the `bws-upsert-secret` skill. It
   prints the secret's UUID.
2. Add an `ExternalSecret` under `apps/<service>/` using that UUID as
   `remoteRef.key`:

   ```yaml
   spec:
     refreshPolicy: CreatedOnce
     secretStoreRef:
       kind: ClusterSecretStore
       name: bitwarden
   ```

`CreatedOnce` means ESO fetches the value once and never polls Bitwarden.
This avoids API rate limits and surprise rotations. To pick up a changed
value, delete the target Secret and ESO recreates it.

---

## Bootstrap

These steps run **after** the `k3s-argocd` playbook has installed k3s and
Argo CD.

### 1. Check the cluster and Argo CD

```sh
kubectl config current-context          # the homelab cluster, not something else
kubectl get nodes
kubectl -n argocd get pods              # everything Running / Ready
```

### 2. Log in to Argo CD (optional, but useful for watching the sync)

```sh
# Initial admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo

# UI at https://localhost:8080 (user: admin)
kubectl -n argocd port-forward svc/argocd-server 8080:443

# CLI login, in another terminal
argocd login localhost:8080 --username admin --insecure
```

Change the admin password once logged in, then delete
`argocd-initial-admin-secret`.

### 3. Push this repo

Argo CD pulls from `https://github.com/alextio/my-homelab-k8s`, not from
your working copy. Commit and push to `main` before going further. If the
repo is private, register credentials first:

```sh
argocd repo add https://github.com/alextio/my-homelab-k8s \
  --username <github-user> --password <github-token>
```

### 4. Create the Bitwarden access token Secret

See [Secrets Management](#secrets-management). Without it the
ClusterSecretStore stays not-ready and every `ExternalSecret` fails.

### 5. Apply the root Application

This is the only manual `kubectl apply`. Everything after it comes from Git.

```sh
kubectl apply -f app-of-apps.yaml
```

The `root` Application syncs `manifests/` and creates one child
`Application` per file in it.

### 6. Watch the sync

```sh
kubectl -n argocd get applications -w
# or
argocd app list
argocd app get <name>
```

All of them should end up `Synced` / `Healthy`, in the order shown in
[Sync Waves](#sync-waves).

### 7. Verify the services

```sh
# Traefik should get the reserved MetalLB address 192.168.6.200
kubectl -n traefik get svc traefik

kubectl get gateway,httproute -A
kubectl get clustersecretstore,externalsecret -A
```

The HTTPRoutes use `*.lan` hostnames. Each name needs a DNS record (AdGuard
Home, or `/etc/hosts`) pointing at the Traefik IP. To test without DNS, send
the Host header to the Traefik IP directly:

```sh
curl -k -H 'Host: grafana.lan'    https://192.168.6.200/
curl -k -H 'Host: prometheus.lan' https://192.168.6.200/
curl -k -H 'Host: traefik.lan'    https://192.168.6.200/
```

`-k` is needed until the homelab root CA is trusted on the client. To export it:

```sh
kubectl -n cert-manager get secret homelab-root-ca \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > homelab-root-ca.crt
```

Grafana's admin password:

```sh
kubectl -n monitoring get secret kube-prometheus-stack-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

---

## Day-to-day

- **Change a service:** edit `apps/<name>/` or `manifests/<name>.yaml`,
  commit, push. Argo CD picks it up (default poll is ~3 minutes; click
  *Refresh* or run `argocd app get <name> --refresh` to go sooner).
- **Add a service:** add `apps/<name>/` and `manifests/<name>.yaml`, pick a
  sync wave from [AGENTS.md](AGENTS.md#sync-waves), and if it needs a fixed
  IP, claim one in the IP table first. Push.
- **Remove a service:** delete `manifests/<name>.yaml`, push. `prune: true`
  removes the Application and everything it manages.
- **Don't `kubectl edit` managed resources.** `selfHeal: true` reverts
  manual changes to match Git.
- **Changed `app-of-apps.yaml`?** It isn't managed by Argo CD. Re-run
  `kubectl apply -f app-of-apps.yaml`.

## Troubleshooting

```sh
argocd app get <name>                         # sync status + per-resource errors
argocd app diff <name>                        # what Argo CD wants to change
argocd app sync <name>                        # force a sync
kubectl -n argocd logs deploy/argocd-repo-server      # Helm/Git render errors
kubectl -n argocd logs statefulset/argocd-application-controller
```

- **`ComparisonError` / `Unknown`:** usually a bad path, a missing values
  file, or invalid YAML in this repo. The repo-server logs say which.
- **Traefik `EXTERNAL-IP` stuck at `<pending>`:** MetalLB isn't healthy yet
  or the `IPAddressPool` didn't apply. Check `kubectl -n metallb-system get
  ipaddresspool,l2advertisement`.
- **`ExternalSecret` stuck in `SecretSyncedError`:** check
  `kubectl get clustersecretstore bitwarden` first. If it's not Ready, the
  `bitwarden-access-token` Secret is missing or wrong, or the sdk-server
  cert hasn't been issued yet (`kubectl -n external-secrets get certificate`).
- **CRD "metadata.annotations: Too long":** the Application needs
  `ServerSideApply=true` in `syncOptions`.

## Tear down

None of the Applications carry the `resources-finalizer.argocd.argoproj.io`
finalizer, so deleting one with `kubectl` leaves everything it deployed
running. Use the CLI, which cascades by default:

```sh
argocd app delete root --cascade
```

That only removes the child `Application` objects. Delete each child the
same way to remove the workloads behind it:

```sh
argocd app delete <name> --cascade
```

Tearing down the cluster itself is done with the Ansible playbooks, not here.
