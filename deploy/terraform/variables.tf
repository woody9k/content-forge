variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile to use. Empty = default credential chain."
  type        = string
  default     = "quantaview-admin"
}

# ---------------------------------------------------------------------------
# Networking — reuses the existing account VPC/subnet the other project
# hosts live in (tassels-node, quantaview-demo), rather than standing up a
# parallel network. This is purely a shared-network convenience: the new
# instance gets its own security group and is not reachable via any other
# box's SG rules.
# ---------------------------------------------------------------------------

variable "vpc_id" {
  description = "Existing VPC to launch into."
  type        = string
  default     = "vpc-0978a6138acd97a34"
}

variable "subnet_id" {
  description = "Existing public subnet (auto-assigns a public IP, routes to an IGW)."
  type        = string
  default     = "subnet-041a99a0bd9529f58"
}

variable "admin_cidr" {
  description = "CIDR allowed SSH (22) access. Never 0.0.0.0/0."
  type        = string
  default     = "67.235.203.43/32"
}

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = "EC2 instance type. t3.small comfortably covers FastAPI + occasional Claude CLI subprocess load."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  type    = number
  default = 20
}

variable "ssh_public_key_path" {
  description = "Path to the public half of a dedicated keypair for this box."
  type        = string
  default     = "~/.ssh/content-forge-kp.pub"
}

# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

variable "hosted_zone_id" {
  description = "Route53 hosted zone id for socialupkeep.com."
  type        = string
  default     = "Z09909393ULX752LCW2Q"
}

variable "domain_name" {
  description = "Public hostname for Content Forge."
  type        = string
  default     = "write.socialupkeep.com"
}
