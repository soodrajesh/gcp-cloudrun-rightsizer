output "service_url" {
  value = var.image != "" ? google_cloud_run_v2_service.app[0].uri : null
}
output "image_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}"
}

output "target_url" {
  value = var.image != "" ? google_cloud_run_v2_service.target[0].uri : null
}
