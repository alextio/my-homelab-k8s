- **`manifests/templates/<service>.yaml`**: an Argo CD `Application` CR that points at the Helm chart (or raw manifests) and at my values.
- **`apps/<service>/`**: the corresponding configuration the Application consumes, such as `values.yaml`, custom resources, and secrets templates.

**Every Application has an `apps/<service>/values.yaml`**, even if it is empty. When empty, leave a comment saying so and why. This applies to every new Application and manifest. Exception: Applications named `*-crds` don't need one. Argo CD installs all operators and CRDs; nothing is installed by hand with `helm`.

Deploying a service like MetalLB is a learning exercise, and its outcome is these two pieces: a correct Application CR in `manifests/` and matching configuration in `apps/`. Keep each step small and self-contained so it's clear what was learned.

## Manifests are a Helm chart

`manifests/` is a Helm chart: the root Application (`app-of-apps.yaml`) renders `manifests/templates/*.yaml` with `helm template`, so each cluster's root can pass different values. Defaults in `manifests/values.yaml` are prod.

- Every source that points at this repo (the `ref: values` source and any `path: apps/<service>` source) uses `targetRevision: {{ .Values.revision }}`. Never hardcode `HEAD` or a branch name. **Why:** a dev cluster sets `revision` to the branch under test, and a hardcoded child would silently keep reading `main`.
- Third-party charts keep their pinned `targetRevision`.
- A literal `{{` in a template must be escaped as `{{ "{{" }}`.
- Before committing, check the render with `helm template root manifests`.

## Sync waves

Single source of truth for ordering. Root creates child Applications in wave order, but it does not wait for a wave to be Healthy before starting the next. Every `sync-wave` annotation in this repo must use a row below, with a comment pointing back here. Do not invent new numbers.

| Wave | What | Applications |
|------|------|--------------|
| -3 | CRDs only | `gateway-api-crds`, `cert-manager-crds`, `agentgateway-crds` |
| -2 | Operators / controllers | `argocd`, `cert-manager`, `metallb`, `external-secrets` |
| -1 | Ingress (needs cert-manager issuers and MetalLB IPs) | `traefik` |
| 0 | Workloads (default) | `kube-prometheus-stack`, `agentgateway`, `adguard-home`, `dex` |

**Why:** CRDs must be established before any custom resource that uses them, and Argo CD does not wait for a CRD to register within a single sync.

Retries, not waits, make the ordering hold. Every child Application sets `syncPolicy.retry` (`limit: -1`, backoff 10s ×2 up to 5m), so a child whose CRDs or webhooks aren't up yet keeps retrying until they are. Copy that block into every new Application. Don't add an `argoproj.io_Application` health check to `argocd-cm`. **Why:** it makes root wait for each wave to be Healthy, so one app failing for reasons outside this repo (e.g. agentgateway) would block every later wave, including apps that don't depend on it.

**Inside an Application** (resources under `apps/<name>/`): the Helm chart's own resources are wave 0 (the default), so resources that need the chart running (e.g. cert-manager's ClusterIssuers/Certificate, which need the webhook) are wave 1. Only add an in-app wave when a resource needs the chart up first, and say why in a comment on the annotation.

## IP address allocations

Running list of the LAN IPs this repo uses. **Update this table whenever you add, change, or remove a pinned IP** (MetalLB `loadBalancerIPs`, the pool in `apps/metallb/default-pool.yaml`, or any other fixed address), and check it before picking a new one.

MetalLB pool `default-pool` covers `192.168.6.200`–`192.168.6.210`. It must stay outside the router's DHCP range.

| IP | Service | Notes | Defined in |
|----|---------|-------|------------|
| 192.168.6.200 | Traefik | HTTP/HTTPS ingress for all `*.lan` hosts (incl. `adguard.lan`) | `apps/traefik/values.yaml` |
| 192.168.6.201 | AdGuard Home | DNS 53 TCP/UDP, bypasses Traefik; dashboard goes through Traefik | `apps/adguard-home/values.yaml` |
| 192.168.6.202 | agentgateway | AI/LLM gateway proxy, HTTPS 443 as `agentgateway.lan` (own cert, bypasses Traefik), HTTP 8080 | `apps/agentgateway/gateway.yaml` |
| 192.168.6.203–210 | free | still in `default-pool` | `apps/metallb/default-pool.yaml` |

Not verified from the cluster: whether non-cluster devices (DHCP leases, Proxmox VMs/LXCs) hold any address in this range.

## ExternalSecrets

Prefer `ExternalSecret` over a plain `Secret` for anything sensitive. Never commit a `kind: Secret` with real or placeholder credentials; render it from Bitwarden instead. Create the Bitwarden entries with the `bws-upsert-secret` skill and use the printed UUID as `remoteRef.key`.

Secrets come from Bitwarden Secrets Manager through the `bitwarden` ClusterSecretStore (`apps/external-secrets/clustersecretstore-bitwarden.yaml`). Every new `ExternalSecret` must set `spec.refreshPolicy: CreatedOnce` so ESO fetches once and never polls Bitwarden (avoids API rate limits and surprise rotations). Don't add a comment when using `CreatedOnce`. Only deviate when a secret genuinely needs periodic refresh, and only then say why in a comment.

```yaml
spec:
  refreshPolicy: CreatedOnce
  secretStoreRef:
    kind: ClusterSecretStore
    name: bitwarden
```

To pull a changed value on demand, delete the target Secret and ESO recreates it from Bitwarden.
