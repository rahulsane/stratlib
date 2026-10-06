# Deploying the public site to AWS

This folder runs `stratlib web --public` on a small EC2 instance behind Caddy, which serves HTTPS with a
Let's Encrypt certificate. Terraform creates the AWS resources. `refresh.ps1` publishes a new snapshot from
this PC and deploys it.

```
your PC                         S3 release bucket              EC2 t4g.small (Ubuntu 24.04 ARM)
stratlib publish  ──refresh.ps1──▶ release/app.tar.gz  ──SSM──▶ stratlib-refresh
                                  release/stratlib.db           ├─ /opt/stratlib/app      (code)
                                  release/refresh.sh           ├─ /opt/stratlib/public   (snapshot, read-only)
terraform apply ────────────────▶ config/Caddyfile             ├─ systemd: screener → 127.0.0.1:8601
                                                               └─ Caddy :443 → 127.0.0.1:8601
```

The security group allows only ports 80 and 443. There is no SSH key and port 22 stays closed. Shell access
and deploys go through AWS Systems Manager. The instance can read the release bucket and nothing else.

## What it costs

On-demand in us-east-1 this comes to about $17.50 a month: the t4g.small instance ($12.26), the Elastic IP
($3.65) and 20 GB of gp3 storage ($1.60). S3 storage for the release is a few cents. Outbound transfer is
free for the first 100 GB a month. The budget alert emails you at 80% of `monthly_budget_usd` (default $25)
in actual spend and at 100% in forecast spend. The budget covers the whole account, not only this stack.

## One-time setup

You need on this PC:

- the AWS CLI v2, signed in to your account (`aws configure` or `aws configure sso`; set `$env:AWS_PROFILE`
  if you use a named profile)
- the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  for the AWS CLI, if you want a shell on the instance
- Terraform 1.6 or later

### 1. Choose the settings

```powershell
cd deploy\terraform
Copy-Item terraform.tfvars.example terraform.tfvars
notepad terraform.tfvars
```

Set `domain` and `alert_email`. Git ignores `terraform.tfvars`.

To keep the site behind a password, make a bcrypt hash of it and put it in `basic_auth_hash`:

```powershell
..\..\.venv\Scripts\python -m pip install bcrypt
..\..\.venv\Scripts\python -c "import bcrypt, getpass; print(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt()).decode())"
```

Visitors then sign in with `basic_auth_user` (default `demo`) and that password.

### 2. Create the AWS resources

```powershell
terraform init
terraform apply
```

The instance takes 3 to 5 minutes after `apply` finishes to install Python, Caddy and the AWS CLI.
`refresh.ps1` waits for that, so you can go on to the next steps right away.

### 3. Add the DNS record

```powershell
terraform output dns_record
```

Add that A record wherever the domain's DNS lives. If Vercel manages it, use the Vercel dashboard under
Domains, then your domain, then DNS Records. You can also use the CLI:

```powershell
vercel dns add example.com screener A <elastic_ip>
```

Don't add the subdomain to the Vercel project itself. That would send it to Vercel instead of the instance.

### 4. Deploy

From the repository root:

```powershell
.\deploy\refresh.ps1
```

The first run takes a few minutes longer because it builds the Python environment on the instance. Caddy
gets the certificate once the DNS record resolves, usually within a few minutes.

## Updating the site

After the backfill, the screens and `stratlib strategy-backtest`:

```powershell
.\deploy\refresh.ps1
```

This publishes a new snapshot, uploads it with the current code and restarts the app. Open tabs reconnect
after a few seconds. Use `-SkipSnapshot` to redeploy code or Caddy changes without uploading the snapshot again.

The Python environment is rebuilt only when `pyproject.toml` changes.

## Changing the password or the domain

Edit `terraform.tfvars`, then run `terraform apply` and `.\deploy\refresh.ps1 -SkipSnapshot`. Terraform
rewrites `config/Caddyfile` in the bucket and the refresh installs it and reloads Caddy. To open the site to
everyone, set `basic_auth_hash = ""`.

## Troubleshooting

Open a shell on the instance:

```powershell
terraform -chdir=deploy\terraform output -raw shell   # prints the aws ssm start-session command
```

Useful commands there:

```bash
sudo journalctl -u screener -n 100        # app log
sudo journalctl -u caddy -n 100           # Caddy and certificate log
sudo cat /var/log/cloud-init-output.log   # first-boot setup
sudo stratlib-refresh                      # redeploy what is in the bucket
```

If a refresh fails after the code swap, the previous code is still in `/opt/stratlib/app.old`.

## Tearing it down

```powershell
terraform -chdir=deploy\terraform destroy
```

This deletes the instance, the Elastic IP, the bucket and its contents, the IAM role and the budget. Remove
the DNS record afterwards.

## Daily update in the cloud

`daily.tf` replaces the manual `stratlib backfill` and `stratlib screen` runs. At 6:15 pm New York time on
weekdays, EventBridge Scheduler starts a Fargate task. The task downloads the database from S3, runs the
backfill and every strategy's screen, uploads the database back and exits. Nothing runs between jobs.

```
EventBridge Scheduler ──▶ Fargate task (4 vCPU, 16 GB, x86)        S3 data bucket (versioned)
  weekdays 18:15 ET         deploy/job/run_daily.py  ◀── download ── db/stratlib.db
                              stratlib backfill                       │
                              stratlib screen --strategy <each>  ── upload ──▶ (3 days of old versions kept)
                              (optional) publish + refresh the web instance
failure ──▶ EventBridge rule ──▶ SNS ──▶ email to alert_email
```

**The cloud copy is now the primary database.** Your PC's `data\stratlib.db` goes stale unless you pull it.

### Cost

About 20 minutes of a 4 vCPU, 16 GB Fargate task a day. At on-demand us-east-1 prices that is roughly $0.08 a
run, or about $2 a month, plus about $0.30 a month for the versioned 4.3 GB database in S3. These figures are
estimates; check the Fargate pricing page. Raise or lower `daily_vcpu` and `daily_memory_mb` after looking at the
task's real peak memory in CloudWatch Container Insights or the log.

### One-time setup

1. Create the resources, then put the FMP key in the secret (it never goes through Terraform state):

   ```powershell
   cd deploy\terraform
   terraform apply
   aws secretsmanager put-secret-value --secret-id (terraform output -raw daily_secret_arn) --secret-string "your-fmp-key"
   ```

   Confirm the subscription email from AWS that goes to `alert_email`, or failure alerts are not delivered.

2. Build and push the image (needs Docker running):

   ```powershell
   .\deploy\job\build-push.ps1
   ```

3. Seed the bucket with your current database. Stop `stratlib web` and any backfill first:

   ```powershell
   .\deploy\db-sync.ps1 -Direction push
   ```

4. Run it once by hand and watch the log group `/stratlib/daily` in CloudWatch:

   ```powershell
   Invoke-Expression (terraform -chdir=deploy\terraform output -raw daily_run_now)
   ```

From then on it runs on its own. Rebuild the image with `build-push.ps1` after changing the code.

### Using the results on your PC

```powershell
.\deploy\db-sync.ps1 -Direction pull    # stop `stratlib web` first
```

Don't run `stratlib backfill` on the PC and the task at the same time: they share FMP's 750 calls a minute but
not a rate limiter. Don't push a PC database over the cloud one unless you mean to; `push` refuses without `-Force`.

### Options

| Variable | Default | Effect |
|---|---|---|
| `daily_schedule`, `daily_timezone` | `cron(15 18 ? * MON-FRI *)`, `America/New_York` | When it runs. |
| `daily_enabled` | `true` | `false` pauses the schedule. |
| `daily_vcpu`, `daily_memory_mb`, `daily_storage_gb` | 4, 16384, 40 | Task size. The scratch disk must hold the database with room to grow. |
| `daily_publish_site` | `false` | `true` also publishes the public snapshot and refreshes the web instance, as `refresh.ps1` does. The code on the instance is still the last bundle `refresh.ps1` uploaded. |

### Rolling back a bad run

The bucket keeps non-current versions for 3 days. List them with
`aws s3api list-object-versions --bucket <daily_data_bucket> --prefix db/` and restore one with
`aws s3api copy-object` using `--copy-source "<bucket>/db/stratlib.db?versionId=<id>"`.

### When a run fails

An email arrives with the reason. A failed screen does not stop the others, and a failed backfill skips the
screens; the database is uploaded either way, since the backfill resumes per symbol. Read the log with:

```powershell
aws logs tail /stratlib/daily --since 2h --region us-east-1
```

Rerun with the `daily_run_now` command. On holidays the backfill finds no new bars and costs about one call per symbol.
