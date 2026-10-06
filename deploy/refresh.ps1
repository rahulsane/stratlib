<#
.SYNOPSIS
    Publish the snapshot and deploy it, with the current code, to the public screener on EC2.

.DESCRIPTION
    1. Runs `stratlib publish` to write public\stratlib.db (skip with -SkipPublish or -SkipSnapshot).
    2. Packs the code, config.yaml, research/reports.json, research/output and deploy/server into a tarball.
    3. Uploads the tarball, the snapshot (unless -SkipSnapshot) and deploy/server/refresh.sh to the release bucket.
    4. Runs refresh.sh on the instance over SSM and prints its output.

    Needs the AWS CLI v2 and Terraform on PATH, AWS credentials (set $env:AWS_PROFILE if you use a named
    profile), and a `terraform apply` in deploy\terraform. Run it from anywhere:
        .\deploy\refresh.ps1
        .\deploy\refresh.ps1 -SkipPublish   # upload public\stratlib.db as it is
        .\deploy\refresh.ps1 -SkipSnapshot  # code or Caddy change only; keep the snapshot already in S3

.PARAMETER SkipPublish
    Upload public\stratlib.db as it is instead of publishing a new one.

.PARAMETER SkipSnapshot
    Don't publish or upload a snapshot. The instance reinstalls the one already in the bucket.
#>
[CmdletBinding()]
param(
    [switch]$SkipPublish,
    [switch]$SkipSnapshot
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$tfDir = Join-Path $PSScriptRoot 'terraform'
$snapshot = Join-Path $root 'public\stratlib.db'

function Invoke-Native {
    # Runs a native command, returns its stdout, and throws on a non-zero exit code.
    param([string]$What, [scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # stderr from native tools is not an error in itself
    try { $out = & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
    return $out
}

function Step([string]$Text) { Write-Host "== $Text" -ForegroundColor Cyan }

Push-Location $root
try {
    Step 'Reading Terraform outputs'
    $bucket   = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw bucket }
    $instance = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw instance_id }
    $region   = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw region }

    if (-not $SkipPublish -and -not $SkipSnapshot) {
        Step 'Publishing the snapshot'
        Invoke-Native 'stratlib publish' { & (Join-Path $root '.venv\Scripts\stratlib.exe') publish } | Write-Host
    }
    if (-not $SkipSnapshot -and -not (Test-Path $snapshot)) { throw "No snapshot at $snapshot. Run without -SkipPublish." }

    Step 'Packing the code'
    $bundle = Join-Path $env:TEMP 'stratlib-app.tar.gz'
    Remove-Item $bundle -ErrorAction SilentlyContinue
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    Invoke-Native 'tar' {
        & $tar -czf $bundle --exclude=__pycache__ --exclude=*.pyc --exclude=*.egg-info `
            pyproject.toml README.md config.yaml src research/reports.json research/output deploy/server
    }
    $mb = { param($p) '{0:N0} MB' -f ((Get-Item $p).Length / 1MB) }
    if ($SkipSnapshot) { Write-Host "code $(& $mb $bundle), snapshot already in S3" }
    else { Write-Host "code $(& $mb $bundle), snapshot $(& $mb $snapshot)" }

    Step "Uploading to s3://$bucket/release/"
    Invoke-Native 'upload code'     { aws s3 cp $bundle "s3://$bucket/release/app.tar.gz" --region $region --only-show-errors }
    if (-not $SkipSnapshot) {
        Invoke-Native 'upload snapshot' { aws s3 cp $snapshot "s3://$bucket/release/stratlib.db" --region $region --only-show-errors }
    }
    Invoke-Native 'upload script'   { aws s3 cp (Join-Path $PSScriptRoot 'server\refresh.sh') "s3://$bucket/release/refresh.sh" --region $region --only-show-errors }

    Step "Running the refresh on $instance"
    $parameters = @{
        commands = @(
            'cloud-init status --wait > /dev/null 2>&1',
            'if [ ! -x /usr/local/bin/aws ]; then echo "First-boot setup did not finish. End of /var/log/cloud-init-output.log:" >&2; tail -n 40 /var/log/cloud-init-output.log | iconv -f utf-8 -t ascii//TRANSLIT -c >&2; exit 1; fi',
            "/usr/local/bin/aws s3 cp s3://$bucket/release/refresh.sh /usr/local/sbin/stratlib-refresh --only-show-errors",
            'chmod 755 /usr/local/sbin/stratlib-refresh',
            # The Windows AWS CLI fails to print non-ASCII characters, so the output is converted to ASCII here.
            "/usr/local/sbin/stratlib-refresh $bucket > /tmp/stratlib-refresh.log 2>&1; rc=`$?",
            'iconv -f utf-8 -t ascii//TRANSLIT -c /tmp/stratlib-refresh.log; exit $rc'
        )
        executionTimeout = @('1800')
    } | ConvertTo-Json -Compress
    $paramFile = Join-Path $env:TEMP 'stratlib-ssm-params.json'
    [IO.File]::WriteAllText($paramFile, $parameters)   # UTF-8 without a BOM, which the AWS CLI requires

    # A new instance takes a minute or two to register with SSM, so retry for up to 5 minutes.
    $commandId = $null
    for ($attempt = 1; $attempt -le 30 -and -not $commandId; $attempt++) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $commandId = aws ssm send-command --instance-ids $instance --document-name AWS-RunShellScript `
            --comment 'stratlib refresh' --parameters "file://$($paramFile -replace '\\', '/')" `
            --region $region --query Command.CommandId --output text 2>$null
        $ErrorActionPreference = $previous
        if ($LASTEXITCODE -ne 0) {
            $commandId = $null
            if ($attempt -eq 1) { Write-Host 'Waiting for the instance to register with SSM' -NoNewline }
            Write-Host -NoNewline '.'
            Start-Sleep -Seconds 10
        }
    }
    if (-not $commandId) {
        throw 'The instance did not accept the SSM command. Check that it is running and has the instance role.'
    }
    Write-Host ''

    $status = 'Pending'
    $result = $null
    while ($status -in @('Pending', 'InProgress', 'Delayed')) {
        Start-Sleep -Seconds 5
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $json = aws ssm get-command-invocation --command-id $commandId --instance-id $instance --region $region --output json 2>$null
        $ErrorActionPreference = $previous
        if ($LASTEXITCODE -ne 0) { continue }   # the invocation can take a moment to appear
        $result = $json | Out-String | ConvertFrom-Json
        $status = $result.Status
        Write-Host -NoNewline '.'
    }
    Write-Host ''

    if ($result.StandardOutputContent) { Write-Host $result.StandardOutputContent }
    if ($status -ne 'Success') {
        if ($result.StandardErrorContent) { Write-Host $result.StandardErrorContent -ForegroundColor Red }
        throw "Refresh finished with status $status. Open a shell with: terraform -chdir=deploy\terraform output -raw shell"
    }
    Step 'Done'
}
finally {
    Pop-Location
}
