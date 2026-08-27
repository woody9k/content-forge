output "instance_id" {
  value = aws_instance.this.id
}

output "public_ip" {
  value = aws_eip.this.public_ip
}

output "ssh_command" {
  value = "ssh -i ~/.ssh/content-forge-kp ubuntu@${aws_eip.this.public_ip}"
}

output "url" {
  value = "https://${var.domain_name}"
}
