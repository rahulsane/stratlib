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
