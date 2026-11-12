# A tiny Linux server

Use a dedicated account and keep the API bound to localhost. Nginx provides the
public HTTPS endpoint. The examples assume Debian/Ubuntu and systemd.

```sh
sudo useradd --system --home /var/lib/reelish --create-home reelish
sudo install -d -o reelish -g reelish /srv/reelish/Movies /var/lib/reelish
sudo python3 -m venv /opt/reelish/venv
sudo /opt/reelish/venv/bin/pip install git+https://github.com/r0hin/reelish
```

Write a random token to `/etc/reelish.env`:

```sh
sudo sh -c 'umask 077; python3 -c "import secrets; print(\"REELISH_TOKEN=\" + secrets.token_urlsafe(32))" > /etc/reelish.env'
```

Copy `reelish.service` to `/etc/systemd/system/`, then enable it:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now reelish
```

Set the hostname in `nginx.conf`, install it in `/etc/nginx/conf.d/reelish.conf`,
and reload nginx after `sudo nginx -t` succeeds. Point your hostname's DNS record
to the server, allow inbound ports 80/443, then use Certbot's nginx integration to
issue a certificate and redirect HTTP to HTTPS. Keep `proxy_buffering off` so
unfinished movies stream immediately.

```sh
sudo certbot --nginx -d play.example.com --redirect
sudo journalctl -u reelish -n 30
```

The token is required for download and administration endpoints. Playback URLs
are public. Don't put the token in a browser bundle or a URL.
