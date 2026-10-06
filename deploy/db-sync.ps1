<#
.SYNOPSIS
    Copy the database between this PC and the S3 bucket the daily update uses.

.DESCRIPTION
    pull  Download the cloud database to data\stratlib.db, for the local GUI and research scripts. Keeps the old
          file as data\stratlib.db.bak. Stop `stratlib web` first.
    push  Upload data\stratlib.db. Use it once to seed the bucket. It refuses when the bucket already has a
          database, because the daily update has probably added newer data; -Force overrides that.

    Needs the AWS CLI v2 and Terraform on PATH and a `terraform apply` in deploy\terraform:
        .\deploy\db-sync.ps1 -Direction pull
        .\deploy\db-sync.ps1 -Direction push
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('pull', 'push')][string]$Direction,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tfDir = Join-Path $PSScriptRoot 'terraform'
$db = Join-Path $root 'data\stratlib.db'
$key = 'db/stratlib.db'

function Invoke-Native {
    param([string]$What, [scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
    return $out
}

$bucket = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw daily_data_bucket }
$region = Invoke-Native 'terraform output' { terraform "-chdir=$tfDir" output -raw region }

# Exit code 0 when the object exists, 254 when it does not.
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
aws s3api head-object --bucket $bucket --key $key --region $region *> $null
$remoteExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $previous

if ($Direction -eq 'pull') {
    if (-not $remoteExists) { throw "No database at s3://$bucket/$key yet. Seed it with -Direction push." }
    if (Test-Path "$db-wal") {
        Write-Warning 'data\stratlib.db-wal exists, so something has the database open (stratlib web, a backfill). Stop it first.'
        if (-not $Force) { throw 'Pull cancelled. Use -Force if you are sure nothing is using the database.' }
    }
    $tmp = "$db.download"
    Write-Host "== Downloading s3://$bucket/$key" -ForegroundColor Cyan
    Invoke-Native 'download' { aws s3 cp "s3://$bucket/$key" $tmp --region $region --only-show-errors }
    if (Test-Path $db) { Move-Item $db "$db.bak" -Force }
    Remove-Item "$db-wal", "$db-shm" -ErrorAction SilentlyContinue
    Move-Item $tmp $db
    Write-Host "Done. The previous file is data\stratlib.db.bak; delete it once you are happy." -ForegroundColor Green
}
else {
    if ($remoteExists -and -not $Force) {
        throw "s3://$bucket/$key already exists, and the daily update has likely added newer data. Pull instead, or use -Force to overwrite it (the bucket keeps the old version for 3 days)."
    }
    if (-not (Test-Path $db)) { throw "No database at $db." }
    Write-Host '== Folding the write-ahead log into the file and checking it' -ForegroundColor Cyan
    $python = Join-Path $root '.venv\Scripts\python.exe'
    $result = Invoke-Native 'database check' { & $python -I (Join-Path $PSScriptRoot 'db_check.py') $db }
    if ($result -ne 'ok') { throw "The database failed its integrity check: $result" }
    Write-Host "== Uploading to s3://$bucket/$key" -ForegroundColor Cyan
    Invoke-Native 'upload' { aws s3 cp $db "s3://$bucket/$key" --region $region --only-show-errors }
    Write-Host 'Done.' -ForegroundColor Green
}
