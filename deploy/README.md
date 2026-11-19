# Deploying Heraldesk on a VPS

Two supported layouts. Both keep Heraldesk on loopback and terminate TLS in a reverse
proxy. Use a long random admin token; never reuse `secret.key`.

## Docker Compose (recommended)

```bash
git clone https://github.com/abengkris/heraldesk.git
cd heraldesk
cat > .env <<'EOF'
HERALDESK_ADMIN_TOKEN=replace-with-a-long-random-value
EOF
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

Compose binds `127.0.0.1:8000` on the host and stores the data folder in the
`heraldesk-data` volume. Put Caddy or Nginx in front:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/desk.example.com/your.domain/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The first start creates the layout, key and database. Then:

```bash
docker compose exec heraldesk heraldesk doctor
docker compose exec heraldesk heraldesk profile new acme --name "Acme" --type company --timezone UTC --languages en
```

Backups stay inside the volume; copy them out with
`docker compose cp heraldesk:/data/backups ./backups`.

## systemd (source install)

```bash
sudo useradd --system --home /var/lib/heraldesk --shell /usr/sbin/nologin heraldesk
sudo -u heraldesk python3.12 -m venv /opt/heraldesk
sudo -u heraldesk /opt/heraldesk/bin/pip install heraldesk==<version>
sudo -u heraldesk /opt/heraldesk/bin/heraldesk init
```

Create `/etc/heraldesk/heraldesk.env` (mode 600):

```dotenv
HERALDESK_HOME=/var/lib/heraldesk
HERALDESK_ADMIN_TOKEN=replace-with-a-long-random-value
HERALDESK_TRUST_PROXY=1
```

Install `deploy/heraldesk.service`, adjust `ExecStart` to
`/opt/heraldesk/bin/heraldesk serve`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now heraldesk
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## Hardening checklist

- Firewall the Heraldesk port; only the proxy may reach it.
- Keep `HERALDESK_TRUST_PROXY=1` only when TLS terminates at the proxy.
- Use `HERALDESK_TELEGRAM_WEBHOOK_SECRET` for Telegram webhooks.
- Run `heraldesk doctor` after every upgrade.
- Run `heraldesk upgrade` (backs up first) when a release adds migrations.
- Back up with credentials and store the archive off the host.
