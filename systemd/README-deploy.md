# Production deployment

    intelligence.aygchicken.com
      /            -> /var/www/laynes-intelligence   (React build)
      /api/        -> 127.0.0.1:8700                 (FastAPI, laynes-api.service)
      /daily-sales-prediction, /sales-report -> unchanged Streamlit apps

## Publish a frontend change

    ./deploy-frontend.sh          # build + rsync + permissions

## Why the build is served from /var/www and not the repo

nginx runs as www-data and /root is mode 700 on purpose -- it holds .env.
Publishing to a real web root keeps that boundary instead of loosening the
home directory. deploy-frontend.sh sets root:www-data 750/640.

## Install / update the API service

    sudo cp systemd/laynes-api.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now laynes-api

Bound to 127.0.0.1 only; nginx is the sole route in. No secrets in the unit
file -- backend/api.py loads .env itself (mode 600, gitignored).

## Rollback to Streamlit

    sudo cp /root/deploy-backups/<ts>/nginx-intelligence.conf \
            /etc/nginx/sites-available/intelligence
    sudo nginx -t && sudo systemctl reload nginx
    sudo systemctl start laynes-admin-chat
    sudo systemctl stop laynes-api          # optional

laynes-admin-chat is stopped but still installed and enabled; admin_chat.py is
still in the repo. Nothing about the Streamlit path was deleted.
