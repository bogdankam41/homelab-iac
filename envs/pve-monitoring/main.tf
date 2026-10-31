# Environment: pve-monitoring — Prometheus + Alertmanager + Grafana on the home
# Proxmox node (LAN 192.168.1.0/24). Separate state from envs/pve and envs/pve-k3s,
# so applying or destroying monitoring never touches AdGuard or the cluster.
module "monitoring" {
  source = "../../modules/monitoring_vm"

  proxmox_node = "pve"
  vm_name      = "monitoring"
  vm_id        = 210
  vm_ip        = "192.168.1.40"
  gateway_ip   = "192.168.1.1"
  # MAC is pinned: a new random MAC lands in the router's restricted profile
  # with no internet, which breaks the install. Reserve it on the router.
  mac_address = "BC:24:11:A0:30:40"
  # The router resolves reliably right after boot; AdGuard (192.168.1.2) can be
  # put first here once everything is up.
  dns_servers = ["192.168.1.2"]

  cpu_cores = 2
  # Peak in-guest use is ~1.4 GiB (Prometheus ~400 MiB at ~40k series); the
  # rest of 3 GiB was guest page cache pinned on the host. The node runs above
  # 80% RAM, so auto-ballooning keeps the VM near the floor most of the time.
  memory_mb          = 2048
  memory_floating_mb = 1792
  # Ansible has installed qemu-guest-agent; the channel appears after a stop/start.
  qemu_agent_enabled = true
  # TSDB lives on this disk — keep it in step with prometheus_retention_time.
  disk_size = 32

  # envs/pve-k3s already downloads this exact cloud image onto the node; this
  # state just consumes the file instead of managing it a second time.
  download_image = false

  ssh_key_path    = var.ssh_key_path
  ssh_public_keys = var.ssh_public_keys

  # Devices to scrape — the list to edit when something new appears on the LAN.
  monitoring_targets = var.monitoring_targets
  # Blackbox probes and TLS-protected exporters outside the LAN.
  monitoring_probes         = var.monitoring_probes
  monitoring_secure_targets = var.monitoring_secure_targets
  # Both Proxmox nodes and all their guests, via the Proxmox API.
  monitoring_pve_nodes = var.monitoring_pve_nodes
  # Network switches: SNMP where the switch has it, the web UI's API where not.
  monitoring_snmp_targets = var.monitoring_snmp_targets
  monitoring_sks_switches = var.monitoring_sks_switches
  # Video recorder: cameras, recording and the archive, over its DVRIP port.
  monitoring_dvr_recorders = var.monitoring_dvr_recorders
  # Long-term copy of the k3s cluster's metrics (its own Prometheus keeps ~3 days).
  monitoring_federation = var.monitoring_federation
  external_labels       = { site = "home" }
  # The VM's own target, labelled like the devices in monitoring_targets.
  self_labels = { role = "monitoring", site = "home" }

  prometheus_retention_time = var.prometheus_retention_time

  grafana_admin_user     = var.grafana_admin_user
  grafana_admin_password = var.grafana_admin_password
  grafana_dashboards     = var.grafana_dashboards
  # The k3s cluster's own Prometheus (kube-prometheus-stack), for the k3s dashboard.
  grafana_extra_datasources = var.grafana_extra_datasources

  alerts_enabled     = var.alerts_enabled
  alert_thresholds   = var.alert_thresholds
  telegram_bot_token = var.telegram_bot_token
  telegram_chat_id   = var.telegram_chat_id
}
