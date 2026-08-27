# main.tf — one public EC2 host for Content Forge, reachable over the open
# internet so claude.ai / ChatGPT "remote connector" features can reach it.
# Everything is self-contained: its own security group, its own keypair, its
# own DNS record. Nothing here can affect any other box in the account.

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "this" {
  key_name   = "content-forge-kp"
  public_key = file(var.ssh_public_key_path)
}

resource "aws_security_group" "this" {
  name_prefix = "content-forge-"
  description = "Content Forge: public web/MCP, SSH from admin only"
  vpc_id      = var.vpc_id

  lifecycle { create_before_destroy = true }
  tags = { Name = "content-forge" }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.this.id
  description       = "SSH from admin CIDR"
  cidr_ipv4         = var.admin_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.this.id
  description       = "HTTP from the internet (certbot HTTP-01 + redirect to HTTPS)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.this.id
  description       = "HTTPS from the internet - claude.ai / ChatGPT connectors originate here"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.this.id
  description       = "Outbound to package mirrors, ACME, Anthropic API"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type           = var.instance_type
  subnet_id               = var.subnet_id
  vpc_security_group_ids  = [aws_security_group.this.id]
  key_name                = aws_key_pair.this.key_name
  user_data               = file("${path.module}/user_data.sh")
  user_data_replace_on_change = true

  # Without a session token an SSRF in the app could read instance-role
  # creds from IMDSv1; this instance has no role at all, but keep the same
  # baseline the other boxes use.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "content-forge" }
}

# Elastic IP so DNS + SSH survive the nightly stop/start (a stopped instance
# with only an auto-assigned public IP gets a new one on start). Only bills
# (~$3.60/mo) while the instance is stopped; free while running.
resource "aws_eip" "this" {
  instance = aws_instance.this.id
  domain   = "vpc"
  tags     = { Name = "content-forge" }
}

resource "aws_route53_record" "this" {
  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.this.public_ip]
}
