# Coder templates

Terraform templates for Coder (`apps/coder`). Argo CD doesn't deploy them (`templates/` is excluded in `manifests/templates/coder.yaml`); push them to Coder by hand. Coder runs them, once per workspace build, inside the Coder pod, and keeps each workspace's state in its database.

## `kubernetes/`

Each workspace is a Deployment (scaled to 0 when stopped) and a PVC for `/home/coder` in the `coder-workspaces` namespace, on the k3s node `coder-1`.

The isolation comes from the cluster, not the template. See "Coder workspaces" in [AGENTS.md](../../../AGENTS.md#coder-workspaces):

- **Node:** `coder-1` only runs workspace pods. It's tainted `coder-workspaces=true:NoSchedule`, and the template tolerates the taint.
- **Pod Security:** `restricted`. Workspaces run as uid 1000 with no capabilities and no `sudo`, and Docker or Podman can't run inside them.
- **Network:** nothing can connect in. Workspaces can reach DNS, the Coder Service and the public internet, but not the LAN or other cluster Services.
- **Agent URL:** the agent connects to `http://coder.coder.svc.cluster.local`, not `https://coder.lan`, because the NetworkPolicy blocks the LAN.
- **Workspace size:** CPU and memory are parameters you can change on each workspace. The home disk size is fixed when the workspace is created, because local-path volumes can't be resized.

### Push

```bash
coder login https://coder.lan
coder templates push kubernetes -d apps/coder/templates/kubernetes   # from the repo root
```
