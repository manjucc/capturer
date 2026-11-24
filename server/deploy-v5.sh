#!/usr/bin/env bash
# Server-side setup on manjucc for the HTTPS upload + gallery.  Self-contained:
#   1. back up /etc/nginx to ~/nginx-config-backup-<date>.tar.gz
#   2. self-signed TLS cert (395 days — browsers reject longer) for
#      manjucc.com, www.manjucc.com, capture.manjucc.com and the ECS IP
#   3. /var/www/captures (new upload dir, nginx-writable) + migrate the
#      V4 photos from /home/andy/captures (kept as-is); the name list
#      lands as capture-list.txt
#   4. basic-auth user "Capturer" (plaintext password lives in
#      ~/.config/capturer/upload.json on the Pi)
#   5. two vhosts: manjucc.com HTTP->HTTPS redirect + HTTPS site (with a
#      read-only /captures/ alias), and capture.manjucc.com — the gallery,
#      also the WebDAV (PUT+MOVE) upload endpoint via nginx's built-in
#      dav module; everything behind basic auth (photos show people)
#   6. nginx -t + reload, then a daily retention timer deleting photos
#      older than RETAIN_DAYS (capture-list.txt is never deleted)
#   7. smoke tests (needs CAP_PASS set for those)
#
# Run from the Pi:   scp server/deploy-v5.sh manjucc:/tmp/
#                    ssh manjucc 'SUDO_PASS=... CAP_PASS=... bash /tmp/deploy-v5.sh'
# (SUDO_PASS only needed when ssh doesn't have a passwordless sudo; on this
#  box andy's sudo asks for a password, so pass it.)
#
# Idempotent: safe to re-run (files are overwritten, photos re-copied).
# The TLS cert is only re-signed when <30 days of validity remain — an
# unconditional re-sign would break the CA pinned on the Pi — and the
# original pre-V5 backups are never clobbered by a re-run.
set -euo pipefail

CERT_DAYS=395
RETAIN_DAYS=90  # photos older than this are deleted daily; list is kept
CAP_USER=Capturer
# Capturer : apr1 hash — must match the password kept in
# ~/.config/capturer/upload.json on the Pi (keep the two in sync on
# every rotation).
CAP_HASH='$apr1$jqYeEDew$bvWZAVhBd/dAYnYVpni9G.'
ECS_IP=118.178.229.25

s() {
  if [[ -n ${SUDO_PASS:-} ]]; then
    printf '%s\n' "$SUDO_PASS" | sudo -S -p '' "$@"
  else
    sudo "$@"
  fi
}

echo "== 1. backup current nginx config =="
if [[ ! -e /etc/nginx/sites-available/default.pre-v5 ]]; then
  s cp -a /etc/nginx/sites-available/default /etc/nginx/sites-available/default.pre-v5
else
  echo "  default.pre-v5 already exists — keeping the original backup"
fi
STAMP=$(date +%F_%H%M)  # minute precision: a same-day re-run makes a new tar
s tar czf "$HOME/nginx-config-backup-$STAMP.tar.gz" -C / etc/nginx
sudo chown "$USER:$USER" "$HOME/nginx-config-backup-$STAMP.tar.gz"

echo "== 2. self-signed cert (re-signed only when <30 days remain) =="
s mkdir -p /etc/nginx/ssl
if s openssl x509 -checkend 2592000 -noout -in /etc/nginx/ssl/manjucc.crt 2>/dev/null; then
  echo "  cert still valid >30 days — keeping it (the Pi's pinned CA stays intact)"
else
  if [[ -f /etc/nginx/ssl/manjucc.crt ]]; then
    echo "  !! cert rotated — RE-PIN THE PI (otherwise every upload fails TLS):"
    echo "     scp manjucc:/etc/nginx/ssl/manjucc.crt ~/.config/capturer/manjucc-capture.crt"
  fi
  openssl req -x509 -newkey rsa:2048 -sha256 -days "$CERT_DAYS" -nodes \
    -subj "/CN=manjucc.com" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth" \
    -addext "subjectAltName=DNS:manjucc.com,DNS:www.manjucc.com,DNS:capture.manjucc.com,IP:$ECS_IP" \
    -keyout /tmp/manjucc.key -out /tmp/manjucc.crt 2>/dev/null
  s install -m 600 /tmp/manjucc.key /etc/nginx/ssl/manjucc.key
  s install -m 644 /tmp/manjucc.crt /etc/nginx/ssl/manjucc.crt
  rm -f /tmp/manjucc.key /tmp/manjucc.crt
fi
openssl x509 -in /etc/nginx/ssl/manjucc.crt -noout -subject -enddate

echo "== 3. /var/www/captures + migrate the V4 dir =="
s mkdir -p /var/www/captures
if [[ -d /home/andy/captures ]]; then
  s cp -a /home/andy/captures/. /var/www/captures/
fi
# The name list is capture-list.txt (legacy copies may appear as
# captured-list.txt or V4's captured.list).  Seed it only when absent —
# the live copy outranks the stale archive on re-runs — promoting the
# most recent legacy live list over the archive copy the cp above just
# brought in, then drop any legacy-named residue.
if [[ ! -f /var/www/captures/capture-list.txt ]]; then
  if [[ -f /var/www/captures/captured-list.txt ]]; then
    s mv /var/www/captures/captured-list.txt /var/www/captures/capture-list.txt
  elif [[ -f /var/www/captures/captured.list ]]; then
    s mv /var/www/captures/captured.list /var/www/captures/capture-list.txt
  elif [[ -f /home/andy/captures/captured.list ]]; then
    s cp -a /home/andy/captures/captured.list /var/www/captures/capture-list.txt
  fi
fi
s rm -f /var/www/captures/captured.list /var/www/captures/captured-list.txt
s chown www-data:www-data /var/www/captures
s chmod 755 /var/www/captures
s chmod -R a+rX /var/www/captures
ls /var/www/captures | wc -l

echo "== 4. htpasswd + vhosts (staged in a temp dir, installed with sudo install
       — `sudo -S tee` would eat its stdin password and write empty files) =="
stage=$(mktemp -d)
printf '%s:%s\n' "$CAP_USER" "$CAP_HASH" > "$stage/htpasswd-capture"

cat > "$stage/default" <<'NGINX'
# manjucc.com main site — V5: HTTP redirects to HTTPS (self-signed cert).
# Pre-V5 original kept at default.pre-v5 and in ~/nginx-config-backup-*.tar.gz
server {
	listen 80 default_server;
	listen [::]:80 default_server;
	server_name manjucc.com www.manjucc.com _;
	return 301 https://$host$request_uri;
}

server {
	listen 443 ssl default_server;
	listen [::]:443 ssl default_server;
	server_name manjucc.com www.manjucc.com _;

	ssl_certificate     /etc/nginx/ssl/manjucc.crt;
	ssl_certificate_key /etc/nginx/ssl/manjucc.key;
	ssl_protocols       TLSv1.2 TLSv1.3;

	root /var/www/html;
	index index.html index.htm index.nginx-debian.html;

	location / {
		try_files $uri $uri/ =404;
	}

	# V5: read-only view of the capture gallery, works while
	# capture.manjucc.com has no DNS record yet (same content as
	# https://capture.manjucc.com/).
	location = /captures { return 301 /captures/; }
	location /captures/ {
		alias /var/www/captures/;
		autoindex on;
		autoindex_exact_size off;
		autoindex_localtime on;
		auth_basic "manjucc capture";
		auth_basic_user_file /etc/nginx/htpasswd-capture;
	}
}
NGINX

cat > "$stage/capture.manjucc.com" <<'NGINX'
# V5: webcam capture gallery, browsable at https://capture.manjucc.com/
# Uploads: nginx built-in WebDAV — PUT to <name>.part, then MOVE into place,
# both under basic auth.  GET (browsing) also requires auth: the photos show
# people, so keep the gallery non-public.
server {
	listen 80;
	listen [::]:80;
	server_name capture.manjucc.com;
	return 301 https://$host$request_uri;
}

server {
	listen 443 ssl;
	listen [::]:443 ssl;
	server_name capture.manjucc.com;

	ssl_certificate     /etc/nginx/ssl/manjucc.crt;
	ssl_certificate_key /etc/nginx/ssl/manjucc.key;
	ssl_protocols       TLSv1.2 TLSv1.3;

	root /var/www/captures;
	autoindex on;
	autoindex_exact_size off;
	autoindex_localtime on;

	client_max_body_size 10m;

	dav_methods PUT MOVE;
	dav_access user:rw group:r all:r;

	location / {
		auth_basic "manjucc capture";
		auth_basic_user_file /etc/nginx/htpasswd-capture;
	}
}
NGINX

s install -m 644 "$stage/default" /etc/nginx/sites-available/default
s install -m 644 "$stage/capture.manjucc.com" /etc/nginx/sites-available/capture.manjucc.com
s install -m 644 "$stage/htpasswd-capture" /etc/nginx/htpasswd-capture
rm -rf "$stage"
s ln -sf /etc/nginx/sites-available/capture.manjucc.com /etc/nginx/sites-enabled/capture.manjucc.com
ls -la /etc/nginx/sites-available/default /etc/nginx/sites-available/capture.manjucc.com /etc/nginx/htpasswd-capture

echo "== 5. test + reload =="
s nginx -t
s systemctl reload nginx
ss -tln | grep -E ':(80|443)\s'

echo "== 6. photo retention: daily timer deleting captures older than $RETAIN_DAYS days =="
# capture-list.txt is NEVER deleted: it is the historical record of every
# photo ever uploaded, and the Pi's merge semantics (server list wins,
# local additions merged in) would push any pruned lines right back on
# its next whole-file PUT — pruning it would lose history for nothing.
stage2=$(mktemp -d)
cat > "$stage2/capture-retention.service" <<UNIT
[Unit]
Description=Delete webcam captures older than $RETAIN_DAYS days

[Service]
Type=oneshot
User=www-data
ExecStart=/usr/bin/find /var/www/captures -maxdepth 1 -type f -name '*.jpg' -mtime +$((RETAIN_DAYS-1)) -delete
UNIT
cat > "$stage2/capture-retention.timer" <<UNIT
[Unit]
Description=Daily webcam capture retention

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
s install -m 644 "$stage2/capture-retention.service" /etc/systemd/system/capture-retention.service
s install -m 644 "$stage2/capture-retention.timer" /etc/systemd/system/capture-retention.timer
rm -rf "$stage2"
s systemctl daemon-reload
s systemctl enable --now capture-retention.timer
systemctl list-timers --no-pager capture-retention.timer | head -3

if [[ -z ${CAP_PASS:-} ]]; then
  echo "== 7. smoke tests skipped (set CAP_PASS to run them) =="
  exit 0
fi

echo "== 7. smoke tests (server-local, via --resolve) =="
C="curl -s -u $CAP_USER:$CAP_PASS --resolve capture.manjucc.com:443:127.0.0.1 --resolve manjucc.com:443:127.0.0.1 --cacert /etc/nginx/ssl/manjucc.crt"
echo "-- http://manjucc.com/ redirects to https:"
curl -s -o /dev/null -w '  %{http_code} -> %{redirect_url}\n' http://127.0.0.1/
echo "-- https main page:";              $C -o /dev/null -w '  %{http_code}\n' https://manjucc.com/
echo "-- /captures/ without auth (want 401):"
curl -s --resolve manjucc.com:443:127.0.0.1 --cacert /etc/nginx/ssl/manjucc.crt -o /dev/null -w '  %{http_code}\n' https://manjucc.com/captures/
echo "-- /captures/ with auth:";         $C -o /dev/null -w '  %{http_code}\n' https://manjucc.com/captures/
echo "v5 test" > /tmp/zz-v5-test.jpg
echo "-- PUT .part:";                    $C -T /tmp/zz-v5-test.jpg -o /dev/null -w '  %{http_code}\n' https://capture.manjucc.com/zz-v5-test.jpg.part
echo "-- MOVE into place:"
$C -X MOVE -H 'Destination: https://capture.manjucc.com/zz-v5-test.jpg' -H 'Overwrite: T' -o /dev/null -w '  %{http_code}\n' https://capture.manjucc.com/zz-v5-test.jpg.part
echo "-- GET final name:";               $C -o /dev/null -w '  %{http_code}\n' https://capture.manjucc.com/zz-v5-test.jpg
echo "-- unauthenticated PUT (want 401):"
curl -s --resolve capture.manjucc.com:443:127.0.0.1 --cacert /etc/nginx/ssl/manjucc.crt -T /tmp/zz-v5-test.jpg -o /dev/null -w '  %{http_code}\n' https://capture.manjucc.com/zz-noauth.jpg.part
# The dav-created file is owned by www-data in a 755 dir — sudo is needed.
s rm -f /tmp/zz-v5-test.jpg /var/www/captures/zz-v5-test.jpg
echo "done"
