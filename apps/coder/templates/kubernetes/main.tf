# Coder template: a workspace is one pod in coder-workspaces, on the
# dedicated node coder-1, with a PVC for /home/coder.
#
# The isolation lives in the cluster (03-argocd-kubernetes AGENTS.md,
# "Coder workspaces"): restricted Pod Security, a default-deny
# NetworkPolicy, and RBAC that only covers coder-workspaces. This template
# has to fit inside it: non-root, no capabilities, no service-account
# token. No Docker/Podman inside workspaces.

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.20"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
  }
}

# Coder's built-in provisioner runs inside the Coder pod, so this uses its
# in-cluster service account (allowed in coder-workspaces only).
provider "kubernetes" {}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

locals {
  namespace = "coder-workspaces"
  name      = "coder-${data.coder_workspace.me.id}"

  # The agent connects to Coder's Service directly. The access URL
  # (https://coder.lan) resolves to Traefik's LAN IP, which the
  # NetworkPolicy blocks, and would also need the homelab CA in the image.
  agent_url = "http://coder.coder.svc.cluster.local"
  init_script = replace(
    coder_agent.main.init_script,
    trimsuffix(data.coder_workspace.me.access_url, "/"),
    local.agent_url,
  )

  labels = {
    "app.kubernetes.io/name"     = "coder-workspace"
    "app.kubernetes.io/instance" = local.name
    "app.kubernetes.io/part-of"  = "coder"
    "com.coder.workspace.id"     = data.coder_workspace.me.id
    "com.coder.workspace.name"   = data.coder_workspace.me.name
    "com.coder.user.id"          = data.coder_workspace_owner.me.id
    "com.coder.user.username"    = data.coder_workspace_owner.me.name
  }
}

data "coder_parameter" "cpu" {
  name         = "cpu"
  display_name = "CPU limit"
  type         = "number"
  default      = "2"
  mutable      = true
  option {
    name  = "1 core"
    value = "1"
  }
  option {
    name  = "2 cores"
    value = "2"
  }
  option {
    name  = "4 cores"
    value = "4"
  }
}

# Memory is requested in full so workspaces can't overcommit coder-1's RAM.
data "coder_parameter" "memory" {
  name         = "memory"
  display_name = "Memory"
  type         = "number"
  default      = "2"
  mutable      = true
  option {
    name  = "2 GiB"
    value = "2"
  }
  option {
    name  = "4 GiB"
    value = "4"
  }
  option {
    name  = "6 GiB"
    value = "6"
  }
}

# Fixed at creation: local-path volumes can't be resized, and changing the
# size would replace the PVC and delete the home directory.
data "coder_parameter" "home_disk_size" {
  name         = "home_disk_size"
  display_name = "Home disk size (GiB)"
  type         = "number"
  default      = "20"
  mutable      = false
  validation {
    min = 5
    max = 100
  }
}

resource "coder_agent" "main" {
  os   = data.coder_provisioner.me.os
  arch = data.coder_provisioner.me.arch
  dir  = "/home/coder"

  startup_script = <<-EOT
    set -e
    # The PVC starts empty and hides the image's /home/coder.
    if [ ! -f ~/.init_done ]; then
      cp -rT /etc/skel ~ || true
      touch ~/.init_done
    fi
  EOT

  env = {
    GIT_AUTHOR_NAME     = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_AUTHOR_EMAIL    = data.coder_workspace_owner.me.email
    GIT_COMMITTER_NAME  = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_COMMITTER_EMAIL = data.coder_workspace_owner.me.email
  }

  metadata {
    display_name = "CPU"
    key          = "cpu"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Memory"
    key          = "mem"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Home disk"
    key          = "home"
    script       = "coder stat disk --path /home/coder"
    interval     = 60
    timeout      = 1
  }
}

resource "kubernetes_persistent_volume_claim_v1" "home" {
  metadata {
    name      = "${local.name}-home"
    namespace = local.namespace
    labels    = local.labels
  }
  # local-path binds when the pod is scheduled (WaitForFirstConsumer).
  wait_until_bound = false
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "${data.coder_parameter.home_disk_size.value}Gi"
      }
    }
  }
}

resource "kubernetes_deployment_v1" "main" {
  # Stopped workspaces scale to zero; the PVC stays.
  count            = data.coder_workspace.me.start_count
  wait_for_rollout = false
  depends_on       = [kubernetes_persistent_volume_claim_v1.home]

  metadata {
    name      = local.name
    namespace = local.namespace
    labels    = local.labels
  }

  spec {
    replicas = 1
    selector {
      match_labels = {
        "app.kubernetes.io/instance" = local.name
      }
    }
    # The PVC is ReadWriteOnce: never run two pods at once.
    strategy {
      type = "Recreate"
    }

    template {
      metadata {
        labels = local.labels
      }
      spec {
        automount_service_account_token = false
        enable_service_links            = false

        node_selector = {
          "coder-workspaces" = "true"
        }
        toleration {
          key      = "coder-workspaces"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }

        security_context {
          run_as_non_root = true
          run_as_user     = 1000
          run_as_group    = 1000
          fs_group        = 1000
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        container {
          name              = "dev"
          image             = "codercom/enterprise-base:ubuntu-20261112"
          image_pull_policy = "IfNotPresent"
          command           = ["sh", "-c", local.init_script]

          env {
            name  = "CODER_AGENT_TOKEN"
            value = coder_agent.main.token
          }

          # run_as_non_root is repeated here: the provider otherwise sends
          # false for the container, which overrides the pod setting and
          # fails restricted Pod Security.
          security_context {
            run_as_non_root            = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }

          resources {
            requests = {
              cpu    = "250m"
              memory = "${data.coder_parameter.memory.value}Gi"
            }
            limits = {
              cpu    = data.coder_parameter.cpu.value
              memory = "${data.coder_parameter.memory.value}Gi"
            }
          }

          volume_mount {
            name       = "home"
            mount_path = "/home/coder"
          }
        }

        volume {
          name = "home"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.home.metadata[0].name
          }
        }
      }
    }
  }
}
