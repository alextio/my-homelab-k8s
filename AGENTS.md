- **`manifests/templates/<service>.yaml`**: an Argo CD `Application` CR that points at the Helm chart (or raw manifests) and at my values.
- **`apps/<service>/`**: the corresponding configuration the Application consumes, such as `values.yaml`, custom resources, and secrets templates.

**Every Application has an `apps/<service>/values.yaml`**, even if it is empty. When empty, leave a comment saying so and why. This applies to every new Application and manifest. Exception: Applications named `*-crds` don't need one. Argo CD installs all operators and CRDs; nothing is installed by hand with `helm`.

Deploying a service like MetalLB is a learning exercise, and its outcome is these two pieces: a correct Application CR in `manifests/` and matching configuration in `apps/`. Keep each step small and self-contained so it's clear what was learned.

**Confirm before adding any new Application.** Before creating a new `manifests/templates/<service>.yaml`, stop and ask the user, laying out the tradeoffs: what problem it solves, the alternatives (including configuring an existing app, or not adding anything), what it costs to run, and what it adds to maintain and upgrade. This applies even when the Application is only a supporting piece of a larger task (e.g. trust-manager to distribute a CA). Changing existing Applications doesn't need this.

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
| -2 | Operators / controllers | `argocd`, `cert-manager`, `trust-manager`, `duckdns`, `metallb`, `external-secrets`, `cloudnative-pg`, `reloader` |
| -1 | Shared infrastructure: ingress (needs cert-manager issuers and MetalLB IPs) and the shared database (needs the CNPG operator and ESO) | `traefik`, `postgres` |
| 0 | Workloads (default) | `kube-prometheus-stack`, `agentgateway`, `adguard-home`, `dex`, `oauth2-proxy`, `immich`, `coder` |

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
| 192.168.6.202 | agentgateway | AI/LLM gateway proxy, **scaled to zero** (needs x86-64-v2; see `apps/agentgateway/parameters-proxy.yaml`), HTTPS 443 as `agentgateway.lan` and `agentgateway.alextio.duckdns.org` (own certs, bypasses Traefik), HTTP 8080 | `apps/agentgateway/gateway.yaml` |
| 192.168.6.203–210 | free | still in `default-pool` | `apps/metallb/default-pool.yaml` |

Not verified from the cluster: whether non-cluster devices (DHCP leases, Proxmox VMs/LXCs) hold any address in this range.

## Coder workspaces

Coder templates run workspaces as pods in the `coder-workspaces` namespace, on a dedicated node. Workspaces run code from agents, so treat them as untrusted. Keep these in place:

**Currently switched off:** Coder is scaled to zero (`coder.replicaCount: 0` in `apps/coder/values.yaml`), and no node is a workspace node. The former one, `coder-1` (192.168.6.103), left the cluster and is now a standalone agent VM outside this repo. The namespace, RBAC and NetworkPolicy below stay in place, so switching back means joining a workspace node and removing `replicaCount`.

- **Node:** the workspace node is a k3s *agent* (never a server), joined with the cluster's separate agent token (`agent-token` on `k3s-server-1`), not the server token. It has the taint `coder-workspaces=true:NoSchedule` and label `coder-workspaces=true`. Only workspace pods tolerate the taint. Don't add that toleration to other workloads. If you reuse a node name, delete its `<name>.node-password.k3s` Secret in `kube-system` first, or k3s refuses the join.
- **Namespace** (`apps/coder/namespace-workspaces.yaml`): `restricted` Pod Security. Don't lower it, and don't add exceptions for nested containers.
- **Network** (`apps/coder/networkpolicy-workspaces.yaml`): default-deny ingress; egress only to cluster DNS, the Coder pods and the non-private internet. Allowing a LAN range or another namespace makes it reachable from every workspace, and bypasses Traefik/oauth2-proxy for Services in it.
- **RBAC:** Coder's service account manages pods, PVCs and Deployments in `coder-workspaces` only (`coder.serviceAccount.workspaceNamespaces` in `apps/coder/values.yaml`). `workspacePerms` stays `false` for the `coder` namespace, whose Secrets a pod there could mount.
- **Templates** (`apps/coder/templates/`, see its README) set `automountServiceAccountToken: false` on workspace pods, and keep no other Secrets in `coder-workspaces`. They're Terraform that Coder runs, not manifests: Argo CD excludes the directory, and a change only takes effect after `coder templates push`. Check with `tofu validate` first. Don't loosen the cluster to fit a template.

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

To pull a changed value on demand, delete the target Secret and ESO recreates it from Bitwarden. Reloader then restarts the workloads that use it, as long as they're opted in. **When you add an `ExternalSecret`, add its target Secret to the consuming workload's Reloader annotation** (see [Reloader](#reloader)).

## Reloader

Reloader (`apps/reloader/`) rolls a Deployment/StatefulSet/DaemonSet when a Secret or ConfigMap it names changes, including the ESO delete-and-recreate above. Without it, a changed secret value only takes effect after a manual `kubectl rollout restart`. It is opt-in per workload (`autoReloadAll: false`).

**Reloader only watches the namespaces listed in `reloader.namespaces` in `apps/reloader/values.yaml`.** Scoped mode (`watchGlobally: false`) gives it a Role per listed namespace and no ClusterRole. **Why:** Reloader can read every Secret it watches, so cluster-wide mode would let a compromised Reloader read every Secret in the cluster, including the Bitwarden token and Argo CD's credentials. **Keep that list exactly matching the namespaces in the opted-in table below:**
- Add the namespace when a workload in a new namespace opts in. Reloader silently ignores annotations in namespaces it doesn't watch, so the opt-in looks right in Git and never fires.
- Remove it when the last opted-in workload in that namespace opts out or its Application is removed, so Reloader doesn't keep Secret access it no longer needs.
- Don't switch back to `watchGlobally: true` or use `namespaceSelector` instead: both need cluster-wide Secret read access.

**Every new service that reads a Secret or ConfigMap at startup opts in.** When adding or changing a service:

1. **List what the pod reads:** every ExternalSecret target in `apps/<svc>/` that is consumed through `envFrom`, `secretKeyRef`, a volume or the chart's `existingSecret`, plus `homelab-root-ca` if the pod mounts it (so a CA rotation reaches it).
2. **Drop what a restart can't fix or doesn't need:**
   - Chart-rendered config that already has a checksum annotation; the chart rolls the pod for it on sync.
   - Values that only seed state once: a bundled database's password (`immich-credentials`), AdGuard's seeded config. Restarting on a new value just breaks the connection or changes nothing.
   - Apps that re-read the value while running (Argo CD's `argocd-secret` / `$secret:key` references, Grafana's dashboard sidecar).
   - Anything in `kube-system`: k3s owns those Deployments and would drop the annotations. Never add `kube-system` to `reloader.namespaces`.
3. **Annotate the pod template** in `apps/<svc>/values.yaml`, naming each Secret/ConfigMap explicitly (comma-separated, no spaces):

   ```yaml
   # Reloader (apps/reloader/values.yaml): roll when <what> changes.
   podAnnotations:
     secret.reloader.stakater.com/reload: "<svc>-env,<svc>-dex-oidc"
     configmap.reloader.stakater.com/reload: "homelab-root-ca"
   ```

   The key's path depends on the chart: top-level `podAnnotations` for most, `coder.podAnnotations`, `grafana.podAnnotations` under kube-prometheus-stack. Check the chart's `values.yaml`. If a chart has no pod annotations value, use its Deployment annotations value (`deploymentAnnotations` or similar); Reloader checks the workload's own metadata first and falls back to the pod template. For raw manifests in `apps/<svc>/`, put the annotations on `spec.template.metadata.annotations`.
4. **Verify the render** with `helm template x <chart> -f apps/<svc>/values.yaml | grep -B20 reloader.stakater`. The annotations must sit under a Deployment/StatefulSet's `spec.template.metadata`, not on a Service or the Pod of a Helm test.
5. **Add the workload's namespace** (the Application's `destination.namespace`) to `reloader.namespaces` in `apps/reloader/values.yaml` if it isn't there yet, then confirm with `helm template reloader stakater/reloader --version <pinned> -n reloader -f apps/reloader/values.yaml | grep -- --namespaces=`.
6. **Add a row to the table below.** Removing a service or an opt-in reverses steps 5 and 6.

Don't use `reloader.stakater.com/auto: "true"`. It also matches chart-rendered config that already has a checksum annotation, so one sync rolls the pod twice, and it hides which Secret triggers a restart. Changing the annotations themselves rolls the pod once on the next sync.

To test an opt-in: `kubectl -n <ns> delete secret <name>`, then `kubectl -n <ns> get pods -w`. ESO recreates the Secret and a new pod should start without a manual restart.

Workloads opted in. Keep this current, and keep its Namespace column identical to `reloader.namespaces`:

| Workload | Namespace | Secrets | ConfigMaps | Defined in |
|----------|-----------|---------|------------|------------|
| coder | `coder` | `coder-db-url`, `coder-dex-oidc` | `homelab-root-ca` | `apps/coder/values.yaml` |
| dex | `dex` | `dex-env` | | `apps/dex/values.yaml` |
| immich | `immich` | (not `immich-credentials`: seeds the bundled DB) | `homelab-root-ca` | `apps/immich/values.yaml` |
| grafana | `monitoring` | `grafana-dex-oidc` | `homelab-root-ca` | `apps/kube-prometheus-stack/values.yaml` |
| oauth2-proxy | `oauth2-proxy` | `oauth2-proxy` | `homelab-root-ca` | `apps/oauth2-proxy/values.yaml` |

Not opted in on purpose: adguard-home (config is seeded once into its PVC), argocd (re-reads its secrets while running), CoreDNS (`kube-system`, k3s-managed; restart by hand).

## Shared PostgreSQL

One CloudNativePG `Cluster`, `postgres` in namespace `postgres` (`apps/postgres/cluster.yaml`), serves every service. Don't enable a chart's bundled PostgreSQL subchart or add another `Cluster`; give the service a role and a database on the shared one instead. Connect to `postgres-rw.postgres.svc.cluster.local:5432` with `sslmode=require`.

To add service `<svc>` (`apps/postgres/coder.yaml` is the worked example):

1. Create one Bitwarden entry, `<svc>-postgres-password`, with the `bws-upsert-secret` skill (default `alnum` format, since the password goes into connection URLs unescaped).
2. In `apps/postgres/cluster.yaml`, add a `spec.managed.roles` entry: `name: <svc>`, `login: true`, `passwordSecret.name: <svc>-db-role`.
3. Add `apps/postgres/<svc>.yaml` with an `ExternalSecret` rendering `<svc>-db-role` (type `kubernetes.io/basic-auth`, keys `username` and `password`) and a `Database` (`owner: <svc>`, `databaseReclaimPolicy: retain`).
4. In `apps/<svc>/`, add an `ExternalSecret` that reads the same Bitwarden entry and renders whatever the service needs (a URL, or host/user/password keys). Secrets can't be referenced across namespaces, so this second copy is required.

Exception: Immich needs the VectorChord extension, which the standard CNPG image doesn't include, so it keeps its bundled database.
