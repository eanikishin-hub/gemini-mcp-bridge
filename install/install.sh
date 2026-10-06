#!/bin/bash
# Установка моста Gemini на свой сервер (Ubuntu / Debian). Запускать от root из папки репозитория:
#
#   bash install/install.sh gemini.example.ru   — мост со своим адресом: работает в claude.ai, на телефоне,
#                                                 в Claude Code где угодно (нужна A-запись адреса на этот сервер)
#   bash install/install.sh                      — без адреса: только для Claude Code на этом же сервере
#
# Повторный запуск безопасен: пароли и вход в Google не трогает, только обновляет программу.
set -euo pipefail
DOMAIN="${1:-}"
SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/most-gemini
DATA=/var/lib/most-gemini
ENVF=/etc/most-gemini.env
DOSTUP=/root/most-gemini-dostup.txt

[ "$(id -u)" = 0 ] || { echo "Нужен root"; exit 1; }
echo "== пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q python3-venv python3-pip openssl curl git >/dev/null
[ -n "$DOMAIN" ] && apt-get install -y -q nginx certbot python3-certbot-nginx >/dev/null

if [ -n "$DOMAIN" ]; then RUN_AS=gmost; else RUN_AS=root; fi
echo "== пользователь и папки (мост работает от: $RUN_AS)"
[ "$RUN_AS" = gmost ] && { id gmost >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin gmost; }
mkdir -p "$APP" "$DATA/secret"
chown -R "$RUN_AS:$RUN_AS" "$DATA"; chmod 750 "$DATA"; chmod 700 "$DATA/secret"

echo "== программа"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$SRC"

echo "== настройки (пароли создаются один раз)"
if [ ! -f "$ENVF" ]; then
  PORT=8150
  while ss -ltn | grep -q ":$PORT "; do PORT=$((PORT+1)); done
  if [ -n "$DOMAIN" ]; then URL="https://$DOMAIN"; else URL="http://127.0.0.1:$PORT"; fi
  cat > "$ENVF" <<EOF
MOST_GEMINI_STATE=$DATA
MOST_GEMINI_PUBLIC_URL=$URL
MOST_GEMINI_PORT=$PORT
MOST_GEMINI_USER=$RUN_AS
# Пароль страницы подключения коннектора (вводится один раз при добавлении коннектора в claude.ai)
MOST_GEMINI_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)
# Ключ для отправки своих картинок на /upload
MOST_GEMINI_UPLOAD_TOKEN=$(openssl rand -hex 24)
MOST_GEMINI_RELOGIN_HINT="Вход в Google протух. Владельцу: попросить Claude обновить вход (команда gemini-vhod, см. README моста)."
# Предохранитель
GEMINI_IMAGES_PER_DAY=50
GEMINI_IMAGE_PAUSE_MIN=20
GEMINI_IMAGE_PAUSE_MAX=40
GEMINI_TEXT_PAUSE=5
GEMINI_LIMIT_PAUSE_HOURS=6
GEMINI_FILES_KEEP_DAYS=14
GEMINI_IMAGE_MODEL=gemini-flash
GEMINI_IMAGE_MODEL_PRO=gemini-pro
EOF
elif [ -n "$DOMAIN" ]; then
  # раньше ставили без адреса — теперь адрес появился
  sed -i "s#^MOST_GEMINI_PUBLIC_URL=.*#MOST_GEMINI_PUBLIC_URL=https://$DOMAIN#; s#^MOST_GEMINI_USER=.*#MOST_GEMINI_USER=$RUN_AS#" "$ENVF"
fi
chmod 600 "$ENVF"
set -a; . "$ENVF"; set +a

cat > /usr/local/bin/gemini-vhod <<'EOF'
#!/bin/bash
# Вход моста в Google: gemini-vhod --json файл.json | gemini-vhod --paste | gemini-vhod (окно Chrome на рабочем столе)
set -a; . /etc/most-gemini.env; set +a
exec /opt/most-gemini/venv/bin/most-gemini-login "$@"
EOF
chmod 755 /usr/local/bin/gemini-vhod

if [ -n "$DOMAIN" ]; then
  echo "== служба"
  sed "s/^User=.*/User=$RUN_AS/; s/^Group=.*/Group=$RUN_AS/" "$SRC/install/most-gemini.service" > /etc/systemd/system/most-gemini.service
  systemctl daemon-reload
  systemctl enable -q most-gemini
  systemctl restart most-gemini

  echo "== nginx и сертификат для $DOMAIN"
  if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    cat > /etc/nginx/sites-available/most-gemini <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 30m;
    location / {
        proxy_pass http://127.0.0.1:$MOST_GEMINI_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header Connection "";
        proxy_set_header X-Most-Gemini-Vhod "";
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/most-gemini /etc/nginx/sites-enabled/most-gemini
    nginx -t -q && systemctl reload nginx
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect -q
  fi
  nginx -t -q && systemctl reload nginx
fi

{
  echo "Мост Gemini — доступы (только для владельца сервера)"
  if [ -n "$DOMAIN" ]; then
    echo "Адрес коннектора для claude.ai:  https://$DOMAIN/mcp"
    echo "Пароль страницы подключения:     $MOST_GEMINI_PASSWORD"
  else
    echo "Режим без адреса. Подключить к Claude Code на этом сервере:"
    echo "  claude mcp add gemini -s user -e MOST_GEMINI_STATE=$DATA -e MOST_GEMINI_USER=root -- $APP/venv/bin/most-gemini --stdio"
  fi
  echo "Ключ для /upload (своя картинка на доработку): $MOST_GEMINI_UPLOAD_TOKEN"
} > "$DOSTUP"
chmod 600 "$DOSTUP"

echo "== готово"
echo "Доступы записаны в $DOSTUP"
echo "Дальше: вход в Google — gemini-vhod --json <выгрузка кук> (или --paste), затем добавить коннектор."
if [ -n "$DOMAIN" ]; then sleep 3; curl -s "http://127.0.0.1:$MOST_GEMINI_PORT/health"; echo; fi
