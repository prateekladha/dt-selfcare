# ---------------- Autonomous Database (Always Free) ----------------
resource "random_password" "db_admin" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "#_"
}

resource "random_password" "db_app" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "#_"
}

resource "oci_database_autonomous_database" "main" {
  compartment_id           = var.compartment_ocid
  db_name                  = "dtselfcare"
  display_name             = "${var.name}-adb"
  db_workload              = "OLTP"
  db_version               = "23ai"
  is_free_tier             = true
  admin_password           = random_password.db_admin.result
  data_storage_size_in_tbs = 1
  compute_model            = "ECPU"
  compute_count            = 1 # Always Free is fixed-size; scaling = upgrade to paid ADB
  # TLS without wallet; only our VCN (via service gateway) may connect.
  is_mtls_connection_required = false
  whitelisted_ips             = [oci_core_vcn.main.id]
  lifecycle {
    ignore_changes = [admin_password, compute_count, data_storage_size_in_tbs]
  }
}

locals {
  adb_tls_low = [
    for p in oci_database_autonomous_database.main.connection_strings[0].profiles : p.value
    if p.consumer_group == "LOW" && p.tls_authentication == "SERVER" && p.protocol == "TCPS"
  ][0]
}

# ---------------- Streaming (Kafka-compatible) ----------------
resource "oci_streaming_stream_pool" "main" {
  compartment_id = var.compartment_ocid
  name           = "${var.name}-pool"
  kafka_settings {
    auto_create_topics_enable = false
    log_retention_hours       = 48
    num_partitions            = 1
  }
}

resource "oci_streaming_stream" "usage" {
  name               = "usage-events"
  partitions         = var.usage_partitions
  retention_in_hours = 48
  stream_pool_id     = oci_streaming_stream_pool.main.id
}

resource "oci_streaming_stream" "alerts" {
  name               = "usage-alerts"
  partitions         = 1
  retention_in_hours = 48
  stream_pool_id     = oci_streaming_stream_pool.main.id
}

resource "oci_streaming_stream" "payments" {
  name               = "payment-events"
  partitions         = 1
  retention_in_hours = 48
  stream_pool_id     = oci_streaming_stream_pool.main.id
}

# ---------------- Object Storage: raw usage archive ----------------
data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.tenancy_ocid
}

resource "oci_objectstorage_bucket" "raw" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.ns.namespace
  name           = "${var.name}-usage-raw"
  access_type    = "NoPublicAccess"
  versioning     = "Disabled"
}

resource "oci_objectstorage_object_lifecycle_policy" "raw" {
  namespace = data.oci_objectstorage_namespace.ns.namespace
  bucket    = oci_objectstorage_bucket.raw.name
  rules {
    name        = "archive-after-30d"
    action      = "ARCHIVE"
    is_enabled  = true
    time_amount = 30
    time_unit   = "DAYS"
    target      = "objects"
  }
  rules {
    name        = "delete-after-400d"
    action      = "DELETE"
    is_enabled  = true
    time_amount = 400
    time_unit   = "DAYS"
    target      = "objects"
  }
  depends_on = [oci_identity_policy.objectstorage_service]
}
