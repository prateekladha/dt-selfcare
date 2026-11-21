# Vault with a software-protected key (no charge). Secrets generated here are platform-internal;
# third-party secrets (Razorpay, Mistral) are added by scripts/put-secrets.sh so they never touch
# Terraform state.
resource "oci_kms_vault" "main" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.name}-vault"
  vault_type     = "DEFAULT"
}

resource "oci_kms_key" "main" {
  compartment_id      = var.compartment_ocid
  display_name        = "${var.name}-key"
  management_endpoint = oci_kms_vault.main.management_endpoint
  protection_mode     = "SOFTWARE"
  key_shape {
    algorithm = "AES"
    length    = 32
  }
}

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "random_password" "demo" {
  length  = 14
  special = false
}

resource "random_password" "source_token" {
  length  = 40
  special = false
}

resource "random_password" "llm_gateway_key" {
  length  = 40
  special = false
}

resource "random_password" "admin_token_secret" {
  length  = 40
  special = false
}

locals {
  stream_username = "${data.oci_identity_tenancy.this.name}/${oci_identity_user.svc.name}/${oci_streaming_stream_pool.main.id}"
  secrets = {
    "db-admin-password"       = random_password.db_admin.result
    "db-app-password"         = random_password.db_app.result
    "db-dsn"                  = local.adb_tls_low
    "stream-username"         = local.stream_username
    "stream-password"         = oci_identity_auth_token.svc.token
    "jwt-secret"              = random_password.jwt.result
    "demo-password"           = random_password.demo.result
    "collector-source-tokens" = jsonencode({ "emu-mediation-01" = random_password.source_token.result, "emu-loadgen-01" = random_password.source_token.result })
    "source-token"            = random_password.source_token.result
    "llm-gateway-key"         = "sk-${random_password.llm_gateway_key.result}"
  }
}

data "oci_identity_tenancy" "this" {
  tenancy_id = var.tenancy_ocid
}

resource "oci_vault_secret" "platform" {
  for_each       = local.secrets
  compartment_id = var.compartment_ocid
  vault_id       = oci_kms_vault.main.id
  key_id         = oci_kms_key.main.id
  secret_name    = "${var.name}-${each.key}"
  secret_content {
    content_type = "BASE64"
    content      = base64encode(each.value)
  }
}
