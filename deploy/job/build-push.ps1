<#
.SYNOPSIS
    Build the daily-update image and push it to ECR.

.DESCRIPTION
    Needs Docker running, the AWS CLI signed in, and a `terraform apply` in deploy\terraform. Run it from anywhere:
        .\deploy\job\build-push.ps1
    The task definition uses the `latest` tag, so the next scheduled run picks up the new image.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tfDir = Join-Path $root 'deploy\terraform'

function Invoke-Native {
    param([string]$What, [scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
    return $out
}

$repo   = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw daily_ecr_repository }
$region = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw region }
$registry = $repo.Split('/')[0]

Write-Host "== Logging in to $registry" -ForegroundColor Cyan
$password = Invoke-Native 'ecr get-login-password' { aws ecr get-login-password --region $region }
$password | docker login --username AWS --password-stdin $registry | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'docker login failed.' }

Write-Host "== Building" -ForegroundColor Cyan
Invoke-Native 'docker build' { docker build --platform linux/amd64 -f (Join-Path $root 'deploy\job\Dockerfile') -t "${repo}:latest" $root } | Out-Host

Write-Host "== Pushing ${repo}:latest" -ForegroundColor Cyan
Invoke-Native 'docker push' { docker push "${repo}:latest" } | Out-Host
Write-Host 'Done.' -ForegroundColor Green
