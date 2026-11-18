# One personal, run-owned AWS source. No shared VPC, EKS, RDS or Sky resource.
# Secret VALUES are provisioned outside Terraform and never enter its state.
terraform {
  required_version = ">= 1.15, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.67.0" }
  }
  backend "local" {}
}

variable "run_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]{8,12}$", var.run_id))
    error_message = "A fresh 8..12 character run ID is required."
  }
}
variable "operator_arn" { type = string }
variable "expires_at" {
  type = string
  validation {
    condition     = can(formatdate("YYYY-MM-DD hh:mm:ss", var.expires_at))
    error_message = "An explicit RFC3339 UTC deadline is required."
  }
}
variable "ami_id" {
  type = string
  validation {
    condition     = can(regex("^ami-[a-f0-9]{17}$", var.ami_id))
    error_message = "Resolve and record the exact Amazon ARM64 AMI before planning."
  }
}

provider "aws" {
  region              = "ap-northeast-2"
  profile             = "default"
  allowed_account_ids = ["265233844540"]
  default_tags {
    tags = {
      Project   = "sova-user-journey"
      RunId     = var.run_id
      OwnerArn  = var.operator_arn
      ExpiresAt = var.expires_at
      component = "source"
    }
  }
}

data "aws_caller_identity" "operator" {}
data "aws_ami" "source" {
  owners = ["amazon"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}
data "aws_availability_zones" "available" { state = "available" }

locals {
  name = "sova-journey-${var.run_id}"
}

resource "aws_vpc" "source" {
  cidr_block           = "10.245.0.0/24"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.name }
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.operator.arn == var.operator_arn
      error_message = "The operator changed. Stop instead of using another credential."
    }
  }
}
resource "aws_internet_gateway" "source" { vpc_id = aws_vpc.source.id }
resource "aws_subnet" "source" {
  vpc_id                  = aws_vpc.source.id
  cidr_block              = "10.245.0.0/27"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
}
resource "aws_route_table" "source" {
  vpc_id = aws_vpc.source.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.source.id
  }
}
resource "aws_route_table_association" "source" {
  subnet_id      = aws_subnet.source.id
  route_table_id = aws_route_table.source.id
}
resource "aws_security_group" "source" {
  name        = local.name
  description = "No inbound ports; SSM outbound and approved HTTPS downloads only"
  vpc_id      = aws_vpc.source.id
  # No ingress rules. Neither SSH, application nor PostgreSQL is public.
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSM, private artifacts, pinned image registry and OS repositories"
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name}-265233844540"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_secretsmanager_secret" "source" {
  name                    = "sova/qualification/${var.run_id}/source"
  recovery_window_in_days = 7
}
resource "aws_ecr_repository" "source" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration { encryption_type = "AES256" }
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_iam_role" "source" {
  name = local.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.source.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "artifacts" {
  role = aws_iam_role.source.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/source/*" },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.source.arn },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = aws_ecr_repository.source.arn }
  ] })
}
resource "aws_iam_instance_profile" "source" { role = aws_iam_role.source.name }

resource "aws_instance" "source" {
  ami                                  = data.aws_ami.source.id
  instance_type                        = "t4g.small"
  subnet_id                            = aws_subnet.source.id
  vpc_security_group_ids               = [aws_security_group.source.id]
  associate_public_ip_address          = true
  iam_instance_profile                 = aws_iam_instance_profile.source.name
  instance_initiated_shutdown_behavior = "stop"
  monitoring                           = false
  credit_specification { cpu_credits = "standard" }
  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 16
    delete_on_termination = false
  }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
    http_protocol_ipv6          = "disabled"
  }
  user_data_replace_on_change = false
  user_data                   = <<-SCRIPT
    #!/bin/bash
    set -euo pipefail
    umask 077
    install -d -m 0700 /var/lib/sova-journey
    printf '%s\n' '${var.run_id}' > /var/lib/sova-journey/run-id
    # An absolute, persistent compute deadline; reboot never adds eight hours.
    cat > /etc/systemd/system/sova-expiry.service <<'UNIT'
    [Unit]
    Description=Stop this owned qualification instance at its deadline
    [Service]
    Type=oneshot
    ExecStart=/usr/bin/systemctl poweroff
    UNIT
    cat > /etc/systemd/system/sova-expiry.timer <<'UNIT'
    [Unit]
    Description=Persistent Sova qualification deadline
    [Timer]
    OnCalendar=${formatdate("YYYY-MM-DD hh:mm:ss", var.expires_at)} UTC
    Persistent=true
    Unit=sova-expiry.service
    [Install]
    WantedBy=timers.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now sova-expiry.timer
    systemctl enable --now amazon-ssm-agent
  SCRIPT
  tags                        = { Name = local.name }
  lifecycle {
    precondition {
      condition     = timecmp(var.expires_at, timestamp()) > 0 && timecmp(var.expires_at, timeadd(timestamp(), "8h")) <= 0
      error_message = "The source deadline must be in the future and at most eight hours away."
    }
  }
  depends_on = [aws_route_table_association.source, aws_iam_role_policy_attachment.ssm]
}

output "source_instance" { value = aws_instance.source.id }
output "source_secret" { value = aws_secretsmanager_secret.source.arn }
output "artifact_bucket" { value = aws_s3_bucket.artifacts.id }
output "image_repository" { value = aws_ecr_repository.source.repository_url }
