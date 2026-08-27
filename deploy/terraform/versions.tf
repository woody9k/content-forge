terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state on purpose — this is a single self-contained box with a
  # single operator. Do not commit terraform.tfstate*; see .gitignore.
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile
}
