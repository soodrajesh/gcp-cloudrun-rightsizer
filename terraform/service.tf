resource "google_service_account" "app" {
  account_id   = "rs-app"
  display_name = "rightsizer runtime"
}
resource "google_project_iam_member" "app_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}
resource "google_project_iam_member" "app_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}
# Read-only access for live mode: Cloud Run configuration and Cloud Monitoring metrics.
resource "google_project_iam_member" "app_run_viewer" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.app.email}"
}
resource "google_project_iam_member" "app_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_cloud_run_v2_service" "app" {
  count               = var.image != "" ? 1 : 0
  name                = "rightsizer"
  location            = var.region
  deletion_protection = false
  labels              = { env = "demo", owner = "platform" }
  template {
    service_account                  = google_service_account.app.email
    timeout                          = "300s"
    max_instance_request_concurrency = 20
    scaling {
      min_instance_count = 0
      max_instance_count = 1 # results live in memory, so there must be one instance
    }
    containers {
      image = var.image
      resources {
        limits   = { cpu = "2", memory = "2Gi" } # parallel simulation workers
        cpu_idle = false                         # comparison jobs keep running after the request that started them returns
      }
      startup_probe {
        http_get { path = "/healthz" }
      }
      env {
        name  = "RIGHTSIZE_WORKERS"
        value = "2"
      }
      env {
        name  = "RIGHTSIZE_LIVE_PROJECTS"
        value = var.project_id
      }
      env {
        name  = "RIGHTSIZE_LLM"
        value = "vertex"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "RIGHTSIZE_ALLOWED_PROVIDERS"
        value = "vertex,none"
      }
    }
  }
  depends_on = [
    google_project_iam_member.app_vertex,
    google_project_iam_member.app_run_viewer,
    google_project_iam_member.app_monitoring_viewer,
  ]
}

# Private: only the admin may invoke. No allUsers binding.
resource "google_cloud_run_v2_service_iam_member" "admin_invoker" {
  count    = var.image != "" ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "user:${var.admin_email}"
}

# A small target with a known configuration, used by validation/ to compare the simulator with real metrics.
resource "google_cloud_run_v2_service" "target" {
  count               = var.image != "" ? 1 : 0
  name                = "rs-target"
  location            = var.region
  deletion_protection = false
  labels              = { env = "demo", owner = "platform" }
  template {
    service_account                  = google_service_account.app.email
    timeout                          = "30s"
    max_instance_request_concurrency = 4
    scaling {
      min_instance_count = 0
      max_instance_count = 10
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec uvicorn rightsize.demo_target:app --host 0.0.0.0 --port $${PORT}"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "target_invoker" {
  count    = var.image != "" ? 1 : 0
  name     = google_cloud_run_v2_service.target[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "user:${var.admin_email}"
}
