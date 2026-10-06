output "region" {
  value = var.region
}

output "bucket" {
  description = "Release bucket that deploy/refresh.ps1 uploads to."
  value       = aws_s3_bucket.release.id
}

output "instance_id" {
  value = aws_instance.app.id
}

output "elastic_ip" {
  description = "Point the A record for var.domain at this address."
  value       = aws_eip.app.public_ip
}

output "dns_record" {
  description = "The record to add in Vercel (or wherever the domain's DNS lives)."
  value       = "A  ${split(".", var.domain)[0]}  ${aws_eip.app.public_ip}"
}

output "shell" {
  description = "Open a shell on the instance through SSM. Needs the Session Manager plugin for the AWS CLI."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.region}"
}
