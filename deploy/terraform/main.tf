data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

# Canonical publishes the current Ubuntu 24.04 ARM image ID here.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

locals {
  bucket = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# Release bucket: the code bundle, the snapshot, the refresh script and the Caddyfile.

resource "aws_s3_bucket" "release" {
  bucket        = local.bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "release" {
  bucket                  = aws_s3_bucket.release.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "release" {
  bucket = aws_s3_bucket.release.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_object" "caddyfile" {
  bucket       = aws_s3_bucket.release.id
  key          = "config/Caddyfile"
  content_type = "text/plain"
  content = templatefile("${path.module}/Caddyfile.tftpl", {
    domain          = var.domain
    basic_auth_user = var.basic_auth_user
    basic_auth_hash = var.basic_auth_hash
  })
}

# Network: only HTTP and HTTPS come in. Shell access goes through SSM, so port 22 stays closed.

resource "aws_security_group" "web" {
  name        = "${var.name}-web"
  description = "HTTP and HTTPS to Caddy"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.web.id
  description       = "port ${each.value} from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.web.id
  description       = "outbound for apt, PyPI, S3, SSM and Lets Encrypt"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Instance role: SSM for shell access and run-command, plus read access to the release bucket.

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "read_release" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.release.arn]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.release.arn}/*"]
  }
}

resource "aws_iam_role_policy" "read_release" {
  name   = "read-release-bucket"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.read_release.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-instance"
  role = aws_iam_role.app.name
}

# The server.

resource "aws_instance" "app" {
  ami                    = nonsensitive(data.aws_ssm_parameter.ubuntu.value)
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.app.name
  vpc_security_group_ids = [aws_security_group.web.id]

  # A public IPv4 address from launch, so first-boot setup can reach apt, PyPI and S3. The Elastic IP
  # replaces it once it is attached.
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    bucket = aws_s3_bucket.release.id
  })
  # User data only runs on an instance's first boot, so a changed script needs a new instance.
  user_data_replace_on_change = true

  # Standard mode caps CPU at the baseline when credits run out instead of billing for surplus credits.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_size_gb
    encrypted   = true
  }

  tags = {
    Name = var.name
  }

  lifecycle {
    # A new Ubuntu image ID would otherwise replace the instance on the next apply.
    ignore_changes = [ami]
  }
}

resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = var.name
  }
}

# Cost guard for the whole account.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
