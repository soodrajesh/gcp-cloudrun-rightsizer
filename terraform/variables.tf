variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "admin_email" {
  type        = string
  description = "The only human allowed to invoke the service (roles/run.invoker)"
}
variable "image" {
  type    = string
  default = ""
}
variable "model" {
  type    = string
  default = "gemini-2.5-flash"
}
