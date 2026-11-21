data "oci_identity_compartment" "this" {
  id = var.compartment_ocid
}

locals {
  cpt = "compartment id ${var.compartment_ocid}"
}

# ---- Service identity used by workloads for Kafka (SASL) and image pulls ----
resource "oci_identity_user" "svc" {
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-svc"
  description    = "Workload identity for ${var.name}: stream push/pull, OCIR pull"
  email          = "${var.name}-svc@example.invalid"
}

resource "oci_identity_group" "svc" {
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-svc"
  description    = "Workload identity group for ${var.name}"
}

resource "oci_identity_user_group_membership" "svc" {
  user_id  = oci_identity_user.svc.id
  group_id = oci_identity_group.svc.id
}

resource "oci_identity_auth_token" "svc" {
  user_id     = oci_identity_user.svc.id
  description = "kafka + ocir"
}

resource "oci_identity_user_capabilities_management" "svc" {
  user_id                      = oci_identity_user.svc.id
  can_use_api_keys             = false
  can_use_auth_tokens          = true
  can_use_console_password     = false
  can_use_customer_secret_keys = false
  can_use_smtp_credentials     = false
}

# ---- CI identity (GitHub Actions): push images, deploy to the cluster ----
resource "tls_private_key" "ci" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "oci_identity_user" "ci" {
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-ci"
  description    = "GitHub Actions deployer for ${var.name}"
  email          = "${var.name}-ci@example.invalid"
}

resource "oci_identity_group" "ci" {
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-ci"
  description    = "CI deployers for ${var.name}"
}

resource "oci_identity_user_group_membership" "ci" {
  user_id  = oci_identity_user.ci.id
  group_id = oci_identity_group.ci.id
}

resource "oci_identity_api_key" "ci" {
  user_id   = oci_identity_user.ci.id
  key_value = tls_private_key.ci.public_key_pem
}

resource "oci_identity_auth_token" "ci" {
  user_id     = oci_identity_user.ci.id
  description = "ocir push"
}

# ---- Worker nodes (instance principal): read secrets, write raw archive ----
resource "oci_identity_dynamic_group" "nodes" {
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-nodes"
  description    = "OKE worker nodes of ${var.name}"
  matching_rule  = "ALL {instance.compartment.id = '${var.compartment_ocid}'}"
}

resource "oci_identity_policy" "workloads" {
  compartment_id = var.compartment_ocid
  name           = "${var.name}-workloads"
  description    = "Least-privilege access for ${var.name} workloads and CI"
  statements = [
    "Allow group ${oci_identity_group.svc.name} to use stream-push in ${local.cpt}",
    "Allow group ${oci_identity_group.svc.name} to use stream-pull in ${local.cpt}",
    "Allow group ${oci_identity_group.svc.name} to read repos in ${local.cpt}",
    "Allow dynamic-group ${oci_identity_dynamic_group.nodes.name} to read secret-family in ${local.cpt}",
    "Allow dynamic-group ${oci_identity_dynamic_group.nodes.name} to read buckets in ${local.cpt}",
    "Allow dynamic-group ${oci_identity_dynamic_group.nodes.name} to manage objects in ${local.cpt} where target.bucket.name='${oci_objectstorage_bucket.raw.name}'",
    "Allow group ${oci_identity_group.ci.name} to manage repos in ${local.cpt}",
    "Allow group ${oci_identity_group.ci.name} to use clusters in ${local.cpt}",
    "Allow group ${oci_identity_group.ci.name} to read secret-family in ${local.cpt}",
  ]
}

resource "oci_identity_policy" "ocir_tenancy" {
  # OCIR namespace-level listing is a tenancy permission
  compartment_id = var.tenancy_ocid
  name           = "${var.name}-ocir"
  description    = "Let ${var.name} identities authenticate to OCIR"
  statements = [
    "Allow group ${oci_identity_group.ci.name} to read objectstorage-namespaces in tenancy",
    "Allow group ${oci_identity_group.svc.name} to read objectstorage-namespaces in tenancy",
  ]
}

resource "oci_identity_policy" "objectstorage_service" {
  compartment_id = var.compartment_ocid
  name           = "${var.name}-objectstorage-lifecycle"
  description    = "Lets Object Storage run lifecycle rules (archive/delete) on our buckets"
  statements     = ["Allow service objectstorage-${var.region} to manage object-family in ${local.cpt}"]
}

# ---- Container registry repositories (private) ----
resource "oci_artifacts_container_repository" "repos" {
  for_each       = toset(["core-api", "usage-collector", "usage-aggregator", "network-emulator", "web", "llm-gateway"])
  compartment_id = var.compartment_ocid
  display_name   = "${var.name}/${each.key}"
  is_public      = false
  is_immutable   = false
}
