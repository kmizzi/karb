# Variables for Karb infrastructure

variable "ssh_public_key" {
  description = "SSH public key for EC2 instances"
  type        = string
}

variable "cloudflare_api_key" {
  description = "Cloudflare Global API Key"
  type        = string
  sensitive   = true
}

variable "cloudflare_email" {
  description = "Cloudflare account email"
  type        = string
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed to SSH into servers"
  type        = list(string)
  default     = ["0.0.0.0/0"]  # Restrict this in production
}

variable "dashboard_allowed_cidrs" {
  description = "CIDR blocks allowed to access the dashboard"
  type        = list(string)
  default     = ["0.0.0.0/0"]  # Restrict this in production
}

variable "bot_instance_type" {
  description = "Instance type for bot server"
  type        = string
  default     = "t4g.small"  # ARM-based, good balance of cost/performance
}

variable "proxy_instance_type" {
  description = "Instance type for proxy server"
  type        = string
  default     = "t4g.nano"  # Minimal instance for proxy
}
