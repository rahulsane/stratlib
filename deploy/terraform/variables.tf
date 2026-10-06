variable "region" {
  description = "AWS region for the instance and the release bucket."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for resource names and the Project tag."
  type        = string
  default     = "stratlib"
}

variable "domain" {
  description = "Hostname the site is served on, such as screener.example.com. Point an A record for it at the elastic_ip output."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Must be an ARM (Graviton) type, since the AMI is arm64."
  type        = string
  default     = "t4g.small"
}

variable "volume_size_gb" {
  description = "Root volume size in GB. The OS, venv, code and snapshot use about 5 GB."
  type        = number
  default     = 20
}

variable "basic_auth_user" {
  description = "User name for the site password."
  type        = string
  default     = "demo"
}

variable "basic_auth_hash" {
  description = "bcrypt hash of the site password. Leave empty to serve the site without a password. See deploy/README.md for how to make one."
  type        = string
  default     = ""
  sensitive   = true
}

variable "alert_email" {
  description = "Address that receives the monthly budget alerts."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the account, in US dollars. Alerts go out at 80% of actual spend and 100% of forecast spend."
  type        = number
  default     = 25
}
