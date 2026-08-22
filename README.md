# Season 13 Hotels — Cloud Booking System

A Flask booking application on a load-balanced, auto-scaling AWS
architecture: EC2 + Auto Scaling Group + Application Load Balancer + RDS
MySQL + S3 + Cognito.

**Live URL — use this one, not an individual instance's address:**
`https://season13hotel-alb-1632872629.us-east-1.elb.amazonaws.com`
(self-signed certificate — no purchased domain, so click through the
browser's "not private" warning once).

```
Browser
   |
Season13Hotel-ALB (HTTPS :443, self-signed cert)
   |
Season13Hotel-ASG  (EC2 web servers: nginx -> Gunicorn -> Flask)
   |--> RDS MySQL   (booking data, private, security-group-restricted)
   |--> S3          (uploaded files, via EC2 IAM role — no access keys)
   |--> Cognito     (sign-in for guests + staff, Hosted UI)
```

## Features

- Sign in with AWS Cognito (Hosted UI) — required for every guest and staff action
- Book a room type for a check-in/check-out date range, with live availability
  and optional add-ons (breakfast, airport pickup, room service)
- View your own bookings and payments; admins see everyone's
- Cancel booking (soft-cancel — record kept, status set to `cancelled`)
- Simulated payment with a required Proof of Payment file upload (stored in S3)
- **Admin dashboard** (`/admin`, staff only — members of the Cognito `Admins`
  group): view, edit, and permanently delete any booking
- `/health` endpoint (always returns HTTP 200 while the app is alive) — used
  by the ALB target group's health check

A JSON API is also available under `/api/bookings`. It requires the same
Cognito session cookie as the web UI (sign in via the browser first, then
reuse that cookie with `curl -b`/`-c` or Postman).

## Project structure

```
cloud-booking-system/
├── app.py
├── requirements.txt
├── schema.sql
├── README.md
├── .gitignore
├── .env.example
├── templates/
│   └── admin/        # staff-only views (dashboard, edit)
├── static/
│   └── images/        # hotel photos + logo used by the templates
└── utils/
    ├── db.py     # RDS connection + queries
    └── s3.py     # S3 upload/download (IAM role auth)
```

## Local setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. A `.env` file is already included locally with the RDS/S3 values from
   Details.txt (NOT committed to GitHub — it's in `.gitignore`). Load it
   before running:
   ```bash
   export $(cat .env | xargs)      # macOS/Linux
   ```
   On Windows (PowerShell):
   ```powershell
   Get-Content .env | ForEach-Object { if ($_ -match '^(.*?)=(.*)$') { [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2]) } }
   ```

   > Note: locally your machine won't have an EC2 IAM role, so S3 calls will
   > fail unless your AWS CLI is configured with credentials that have S3
   > access (e.g. `aws configure`). This is expected — S3 upload/download is
   > designed to work via the IAM role once deployed to EC2.

3. Run the app:
   ```bash
   python app.py
   ```
   Visit `http://localhost` (port 80 is HTTP's default, so no `:port` needed
   in the URL).

   > Port 80 is a privileged port on Linux/macOS — `python app.py` there
   > needs `sudo` (or run `PORT=8000 python app.py` and visit
   > `http://localhost:8000` instead, no other changes needed). On Windows
   > this usually isn't restricted, unless something else is already
   > listening on port 80.

## Database connection (matches Details.txt)

The RDS instance requires SSL. If `mysql` CLI access is needed for manual
checks:
```bash
curl -o global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
mysql -h csc3074-db.cxgm5s4nga74.us-east-1.rds.amazonaws.com -P 3306 -u csc3074admin -p --ssl-mode=VERIFY_IDENTITY --ssl-ca=./global-bundle.pem
```
PyMySQL (used by the app) connects over SSL by default when the RDS endpoint
requires it, so no extra config is needed in `utils/db.py` for the app itself.

> **Double-check `DB_NAME`.** Details.txt lists the RDS *instance identifier*
> as `csc3074-db`, but MySQL database (schema) names can't contain hyphens
> unless backticked. Confirm the actual schema name inside MySQL (e.g. run
> `SHOW DATABASES;`) and update `DB_NAME` in `.env` to match — `schema.sql`
> defaults to `csc3074_db` (underscore) as a placeholder.

## AWS Cognito setup (Console)

Sign-in is required for both guests and staff, so this needs to exist before
the app is usable. All of this is one-time setup in the AWS Console.

1. **Create the User Pool.** Cognito → User pools → Create user pool.
   - Sign-in options: **Email**.
   - Password policy / MFA: defaults are fine for this project.
   - Name it something like `season13hotels-users`.
2. **Create an App Client.**
   - App type: **Confidential client** (so it gets a client secret — this
     app runs server-side in Flask, so it can keep a secret safely).
   - Under the client's Hosted UI settings, enable **Authorization code
     grant**, scopes `openid`, `email`, `profile`.
   - Add **Allowed callback URLs**: `http://localhost/callback` for local
     dev, plus `https://season13hotel-alb-1632872629.us-east-1.elb.amazonaws.com/callback`
     for the deployed app (must be `https://` — Cognito rejects plain HTTP
     for anything except `localhost`/`127.0.0.1`).
   - Add **Allowed sign-out URLs**: `http://localhost/` and
     `https://season13hotel-alb-1632872629.us-east-1.elb.amazonaws.com/`.
3. **Set up the Hosted UI domain.** User pool → App integration → Domain →
   pick a domain prefix (e.g. `season13hotels` → becomes
   `season13hotels.auth.us-east-1.amazoncognito.com`).
4. **Create the `Admins` group.** User pool → Groups → Create group, name it
   exactly `Admins` (case-sensitive — the app checks for this literal name
   in the ID token's `cognito:groups` claim).
5. **Make yourself staff.** User pool → Users → create/find your user →
   Add to group → `Admins`. Anyone not in this group is treated as a guest
   (can only see/manage their own bookings).
6. **Fill in `.env`** with what the Console gave you:
   ```
   COGNITO_REGION=us-east-1
   COGNITO_USER_POOL_ID=us-east-1_xxxxxxxxx
   COGNITO_CLIENT_ID=<app client id>
   COGNITO_CLIENT_SECRET=<app client secret>
   COGNITO_DOMAIN=season13hotels.auth.us-east-1.amazoncognito.com
   ```

Once those five values are set, `/login` redirects to the Hosted UI,
`/callback` exchanges the code for tokens and starts the Flask session, and
`/logout` clears the session and signs out of Cognito too. Until they're
set, `/login` shows a flash message instead of crashing (useful if you want
to preview the design locally before Cognito is wired up).

## Deploying (Auto Scaling Group + ALB)

The app runs behind `Season13Hotel-ALB`, which forwards to `Season13Hotel-ASG`
(EC2 instances launched from `Season13Hotel-LT`). The ASG is set to
**min 2 / desired 2 / max 4**, spanning `us-east-1a` and `us-east-1b`, so
there are always two instances running in two Availability Zones — verified
by deliberately terminating one and confirming the group launches and
self-bootstraps a healthy replacement automatically (~2–3 minutes, no manual
steps). **Every instance self-configures on boot** via the launch template's
user-data script — there is no manual per-instance setup. That script:

1. Installs `git`, `python3-pip`, `nginx`.
2. Deletes and re-clones `/home/ec2-user/cloud-booking-system` fresh from
   GitHub (the base AMI has a stale copy baked in from an earlier snapshot —
   always force a clean checkout rather than assuming the directory is empty).
3. Rebuilds the Python venv and installs `requirements.txt`.
4. Writes `/home/ec2-user/cloud-booking-system/.env` with the DB/S3/Cognito
   config.
5. Installs a `cloud-booking.service` systemd unit running
   `gunicorn -w 4 --preload -b 127.0.0.1:8000 app:app` (`--preload` matters —
   see the note in `app.py`'s docstring).
6. Configures nginx to reverse-proxy `:80` to `127.0.0.1:8000`, forwarding
   the **incoming** `X-Forwarded-Proto` header through unchanged
   (`proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;` — **not**
   `$scheme`). This matters because the ALB terminates HTTPS and only speaks
   plain HTTP to instances; using `$scheme` would clobber the ALB's correct
   `https` value with `http`, breaking Cognito's redirect URL.

To ship a code change:
1. Push to `main` on GitHub.
2. SSH into each currently-running ASG instance and `git pull && sudo
   systemctl restart cloud-booking.service` (list them via `aws autoscaling
   describe-auto-scaling-groups --auto-scaling-group-names Season13Hotel-ASG`
   — there are normally two, in different AZs, both need updating).
3. You do **not** need to touch the launch template for an ordinary code
   change — its bootstrap script always clones whatever is currently on
   `main` at boot time, so any *new* instance the ASG launches picks up the
   latest code automatically. Only update the launch template (new version +
   `aws autoscaling start-instance-refresh --auto-scaling-group-name
   Season13Hotel-ASG`) if the bootstrap script itself needs to change.

**One-time setup already done, documented for reference:**
- RDS security group allows inbound `3306` from the ALB/ASG's security group.
- The ALB has an HTTPS `:443` listener using a self-signed certificate
  imported into ACM (`aws acm import-certificate`) — there's no purchased
  domain, so ACM can't issue a real one for `*.elb.amazonaws.com`.
- Cognito's App Client has `https://season13hotel-alb-1632872629.us-east-1.elb.amazonaws.com/callback`
  (and the matching sign-out URL) registered as an allowed redirect.

## Acceptance checklist

- [ ] `pip install -r requirements.txt` works
- [ ] App runs locally
- [ ] Cognito User Pool / App Client / domain / `Admins` group created
- [ ] Guest can sign in via `/login` and create a booking
- [ ] Booking is stored in RDS, owned by the signed-in guest
- [ ] Guest can retrieve/cancel only their own bookings (403 on others')
- [ ] Paying a booking requires a Proof of Payment file; it uploads to S3
- [ ] Proof of Payment can be downloaded back from S3
- [ ] A cancelled booking cannot be paid; only the owning guest (not admins) can pay
- [ ] Staff user (in `Admins` group) can view/edit/delete any booking at `/admin`
- [ ] `/health` returns HTTP 200 and is what the ALB target group checks
- [ ] Terminating an ASG instance triggers an automatic, self-bootstrapped replacement
- [ ] Gunicorn can start the application
- [ ] No AWS access keys or Cognito client secret stored in GitHub
- [ ] `.env` excluded by `.gitignore`