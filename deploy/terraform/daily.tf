# Daily update: an EventBridge schedule starts a Fargate task that runs `stratlib backfill` and every screen against
# the database in S3, then exits. Nothing runs between jobs, so the cost is the minutes the task is up.
# The task's code is deploy/job/run_daily.py.

variable "daily_schedule" {
  description = "EventBridge Scheduler cron for the daily update, in daily_timezone. FMP's end-of-day bar is final after 6 pm Eastern."
  type        = string
  default     = "cron(15 18 ? * MON-FRI *)"
}

variable "daily_timezone" {
  description = "Time zone of daily_schedule. It follows daylight saving time."
  type        = string
  default     = "America/New_York"
}

variable "daily_enabled" {
  description = "Set to false to pause the schedule without destroying anything."
  type        = bool
  default     = true
}

variable "daily_vcpu" {
  description = "Task vCPUs (1, 2, 4, 8 or 16). The weekly base search in the screens uses every core it gets."
  type        = number
  default     = 4
}

variable "daily_memory_mb" {
  description = "Task memory in MB. 4 vCPUs allow 8192 to 30720."
  type        = number
  default     = 16384
}

variable "daily_storage_gb" {
  description = "Task scratch disk in GB (21 to 200). It holds the database, which is 4.3 GB and growing, plus the SQLite log and the snapshot."
  type        = number
  default     = 40
}

variable "daily_publish_site" {
  description = "After the update, publish the snapshot and have the web instance install it, as deploy/refresh.ps1 does by hand."
  type        = bool
  default     = false
}

locals {
  daily_bucket = "${var.name}-data-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# The database. Versioning keeps each night's previous copy for three days, so a bad run can be rolled back.

resource "aws_s3_bucket" "data" {
  bucket        = local.daily_bucket
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket     = aws_s3_bucket.data.id
  depends_on = [aws_s3_bucket_versioning.data]

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 3
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Image registry, log group and the FMP key. Terraform creates the secret but not its value, so the key never
# enters the state: see deploy/README.md for the put-secret-value command.

resource "aws_ecr_repository" "daily" {
  name         = "${var.name}-daily"
  force_delete = true
}

resource "aws_ecr_lifecycle_policy" "daily" {
  repository = aws_ecr_repository.daily.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "daily" {
  name              = "/${var.name}/daily"
  retention_in_days = 30
}

resource "aws_secretsmanager_secret" "fmp" {
  name                    = "${var.name}/fmp-api-key"
  description             = "Financial Modeling Prep API key for the daily update"
  recovery_window_in_days = 0
}

# Roles. The execution role pulls the image, writes logs and reads the secret. The task role is what the code runs as.

data "aws_iam_policy_document" "assume_ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "daily_execution" {
  name               = "${var.name}-daily-execution"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

resource "aws_iam_role_policy_attachment" "daily_execution" {
  role       = aws_iam_role.daily_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "daily_execution_secret" {
  name = "read-fmp-secret"
  role = aws_iam_role.daily_execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.fmp.arn }]
  })
}

resource "aws_iam_role" "daily_task" {
  name               = "${var.name}-daily-task"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

data "aws_iam_policy_document" "daily_task" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.data.arn}/db/*"]
  }

  # Only when the task also refreshes the public site.
  dynamic "statement" {
    for_each = var.daily_publish_site ? [1] : []
    content {
      actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
      resources = ["${aws_s3_bucket.release.arn}/release/stratlib.db"]
    }
  }
  dynamic "statement" {
    for_each = var.daily_publish_site ? [1] : []
    content {
      actions = ["ssm:SendCommand"]
      resources = [
        "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/${aws_instance.app.id}",
        "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
      ]
    }
  }
  dynamic "statement" {
    for_each = var.daily_publish_site ? [1] : []
    content {
      actions   = ["ssm:GetCommandInvocation"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "daily_task" {
  name   = "daily-task"
  role   = aws_iam_role.daily_task.id
  policy = data.aws_iam_policy_document.daily_task.json
}

# The task. It runs in the default VPC with a public IP for FMP, S3 and ECR, and accepts no inbound traffic.

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "daily" {
  name        = "${var.name}-daily"
  description = "Daily update task: outbound only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "daily" {
  security_group_id = aws_security_group.daily.id
  description       = "FMP, S3, ECR and CloudWatch"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_ecs_cluster" "daily" {
  name = "${var.name}-daily"
}

resource "aws_ecs_task_definition" "daily" {
  family                   = "${var.name}-daily"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.daily_vcpu * 1024)
  memory                   = tostring(var.daily_memory_mb)
  execution_role_arn       = aws_iam_role.daily_execution.arn
  task_role_arn            = aws_iam_role.daily_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  ephemeral_storage {
    size_in_gib = var.daily_storage_gb
  }

  container_definitions = jsonencode([{
    name      = "daily"
    image     = "${aws_ecr_repository.daily.repository_url}:latest"
    essential = true
    environment = concat([
      { name = "DATA_BUCKET", value = aws_s3_bucket.data.id },
      { name = "DB_KEY", value = "db/stratlib.db" },
      # The container reports the host's core count, so the screens' worker pool is sized here instead.
      { name = "WORKERS", value = tostring(var.daily_vcpu) },
      ], var.daily_publish_site ? [
      { name = "PUBLISH_BUCKET", value = aws_s3_bucket.release.id },
      { name = "PUBLISH_INSTANCE_ID", value = aws_instance.app.id },
    ] : [])
    secrets = [{ name = "FMP_API_KEY", valueFrom = aws_secretsmanager_secret.fmp.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.daily.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "daily"
      }
    }
  }])
}

# The schedule.

data "aws_iam_policy_document" "assume_scheduler" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "daily_scheduler" {
  name               = "${var.name}-daily-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume_scheduler.json
}

resource "aws_iam_role_policy" "daily_scheduler" {
  name = "run-daily-task"
  role = aws_iam_role.daily_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.daily.family}:*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.daily.arn } }
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.daily_execution.arn, aws_iam_role.daily_task.arn]
      },
    ]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                         = "${var.name}-daily"
  description                  = "Backfill prices and run every screen"
  schedule_expression          = var.daily_schedule
  schedule_expression_timezone = var.daily_timezone
  state                        = var.daily_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.daily.arn
    role_arn = aws_iam_role.daily_scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.daily.arn
      launch_type         = "FARGATE"
      platform_version    = "LATEST"
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.daily.id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# Failure alert: an email when the task stops with a non-zero exit code or never starts.

resource "aws_sns_topic" "daily_alerts" {
  name = "${var.name}-daily-alerts"
}

resource "aws_sns_topic_subscription" "daily_alerts" {
  topic_arn = aws_sns_topic.daily_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_event_rule" "daily_failed" {
  name        = "${var.name}-daily-failed"
  description = "The daily update task failed"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.daily.arn]
      lastStatus = ["STOPPED"]
      "$or" = [
        { containers = { exitCode = [{ "anything-but" = 0 }] } },
        { stopCode = ["TaskFailedToStart"] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "daily_failed" {
  rule = aws_cloudwatch_event_rule.daily_failed.name
  arn  = aws_sns_topic.daily_alerts.arn
  input_transformer {
    input_paths = {
      reason = "$.detail.stoppedReason"
      task   = "$.detail.taskArn"
    }
    input_template = "\"The stratlib daily update failed: <reason>. Task <task>. Logs: CloudWatch log group ${aws_cloudwatch_log_group.daily.name}.\""
  }
}

data "aws_iam_policy_document" "daily_alerts" {
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.daily_alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "daily_alerts" {
  arn    = aws_sns_topic.daily_alerts.arn
  policy = data.aws_iam_policy_document.daily_alerts.json
}

output "daily_data_bucket" {
  description = "Holds the database the daily update reads and writes (db/stratlib.db)."
  value       = aws_s3_bucket.data.id
}

output "daily_ecr_repository" {
  description = "Where deploy/job/build-push.ps1 pushes the image."
  value       = aws_ecr_repository.daily.repository_url
}

output "daily_secret_arn" {
  description = "Put the FMP key here."
  value       = aws_secretsmanager_secret.fmp.arn
}

output "daily_run_now" {
  description = "Start the daily update by hand."
  value = join(" ", [
    "aws ecs run-task --cluster ${aws_ecs_cluster.daily.name} --launch-type FARGATE",
    "--task-definition ${aws_ecs_task_definition.daily.family} --region ${var.region}",
    "--network-configuration \"awsvpcConfiguration={subnets=[${join(",", data.aws_subnets.default.ids)}],securityGroups=[${aws_security_group.daily.id}],assignPublicIp=ENABLED}\"",
  ])
}
