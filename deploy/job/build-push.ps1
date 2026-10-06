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

# A standalone docker.exe can be configured for Docker Desktop's credential helper without having it on PATH.
# Then login fails with "docker-credential-desktop not found". Use a private config without the helper for this
# run, with the same engine, instead of editing ~\.docker\config.json.
if (-not (Get-Command docker-credential-desktop -ErrorAction SilentlyContinue)) {
    $userConfig = Join-Path $env:USERPROFILE '.docker\config.json'
    if ((Test-Path $userConfig) -and ((Get-Content $userConfig -Raw) -match '"credsStore"\s*:\s*"desktop"')) {
        $private = Join-Path $env:TEMP 'stratlib-docker-config'
        New-Item -ItemType Directory -Force $private | Out-Null
        $json = Get-Content $userConfig -Raw | ConvertFrom-Json
        $json.PSObject.Properties.Remove('credsStore')
        [IO.File]::WriteAllText((Join-Path $private 'config.json'), ($json | ConvertTo-Json -Depth 10))
        $env:DOCKER_CONFIG = $private
        if (-not $env:DOCKER_HOST) { $env:DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine' }
    }
}

$repo   = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw daily_ecr_repository }
$region = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw region }
$registry = $repo.Split('/')[0]

Write-Host "== Logging in to $registry" -ForegroundColor Cyan
# cmd does the pipe: Windows PowerShell 5.1 would add a line break to the token and ECR would answer 400.
Invoke-Native 'docker login' { cmd /c "aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry" } | Out-Host

Write-Host "== Building" -ForegroundColor Cyan
Invoke-Native 'docker build' { docker build --platform linux/amd64 -f (Join-Path $root 'deploy\job\Dockerfile') -t "${repo}:latest" $root } | Out-Host

Write-Host "== Pushing ${repo}:latest" -ForegroundColor Cyan
Invoke-Native 'docker push' { docker push "${repo}:latest" } | Out-Host
Write-Host 'Done.' -ForegroundColor Green
