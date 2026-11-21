variable "region" {
  type    = string
  default = "ap-mumbai-1"
}
variable "oci_profile" {
  type    = string
  default = "DEFAULT"
}
variable "tenancy_ocid" { type = string }
variable "compartment_ocid" {
  type        = string
  description = "Dedicated compartment for this system (created by bootstrap.sh) - isolates it from other workloads"
}
variable "name" {
  type    = string
  default = "dt-selfcare"
}
variable "kubernetes_version" {
  type    = string
  default = "v1.37.1"
}
variable "node_count" {
  type        = number
  default     = 1
  description = "Set to 0 to park the platform at near-zero cost; control plane, DB and data stay."
}
variable "node_ocpus" {
  type    = number
  default = 2
}
variable "node_memory_gb" {
  type    = number
  default = 16
}
variable "usage_partitions" {
  type        = number
  default     = 4
  description = "Fixed at stream creation (OCI). Sizing: ~1 partition per 1 MB/s compressed ingest."
}
variable "api_allowed_cidrs" {
  type        = list(string)
  default     = ["0.0.0.0/0"]
  description = "Who may reach the Kubernetes API endpoint (auth is still OCI IAM)."
}
