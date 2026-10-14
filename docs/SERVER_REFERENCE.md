# The Voxie server, by hand

**You probably do not need this page.** `voxie init` sets up a fresh Linux
server over SSH by itself (see [DEPLOY.md](../DEPLOY.md)). This is the long
version, for when you want to do it yourself, run the server at home, move an
existing install, or understand what `init` did.

The gateway (sync, catalogue, uploads) and Audiobookshelf run together with
`docker compose`, on any always-on machine with Docker: a spare Mac or PC, a
Linux box, a NAS (Synology Container Manager, Unraid) or a Raspberry Pi 4/5
(64-bit). Narration happens elsewhere, on your Mac or Linux machine
(`mac_ghost.py`), which uploads finished books here. The gateway needs no GPU
and little memory.

Two ways to run it by hand:

- **At home** (section A): your own machine, reached from anywhere through
  Tailscale Funnel. Free, and nothing of yours leaves your house except
  through the encrypted tunnel.
- **On a rented server** (sections 0-9): a small VPS, about €5 a month. The
  runbook as first deployed for this project on Hetzner.

**HTTPS on a rented server.** `docker-compose.yml` carries an optional Caddy
service (the `https` profile) that fetches and renews a Let's Encrypt
certificate by itself. Point a domain's A record at the server, then add to
its `.env`: `DOMAIN=voxie.example.com`, `COMPOSE_PROFILES=https`,
`GATEWAY_BIND=127.0.0.1:3000`, `ABS_BIND=127.0.0.1:13379` and
`GATEWAY_PUBLIC_URL=https://voxie.example.com`, and `docker compose up -d`.
The gateway is then `https://voxie.example.com` and Audiobookshelf
`https://voxie.example.com:13378`. Open ports 80, 443 and 13378.

---

## A. At home, reached through Tailscale Funnel

Your Kobo cannot run Tailscale, so the gateway needs a public HTTPS address.
Tailscale Funnel gives your machine one, `https://<machine>.<tailnet>.ts.net`,
with a real certificate, on every plan including the free one.

### A1. Start the stack (on the home server)

```bash
git clone <this repository> voxie && cd voxie
cp .env.example .env
# The key that encrypts stored ABS tokens, written straight into the file,
# never printed. Keep a copy in your password manager.
python3 -c "import base64,secrets;print('LIBRA_SECRET_KEY='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" >> .env
docker compose up -d --build
docker compose ps            # both containers up, librarian_server healthy
```

### A2. Funnel

Install Tailscale on the home server **and on the Mac where you narrate**
(the Mac reaches Audiobookshelf over the tailnet), and log both in. In the
admin console turn on MagicDNS and HTTPS certificates, and allow Funnel for
the server (the first `tailscale funnel` command prints a link to do it).
Then, on the server:

```bash
sudo tailscale funnel --bg --https=443  localhost:3000    # the gateway
sudo tailscale funnel --bg --https=8443 localhost:13378   # Audiobookshelf, for your phone
tailscale funnel status                                   # shows both public addresses
```

`--bg` keeps them across reboots. Funnel only listens on 443, 8443 and
10000. If your phone runs the Tailscale app you can skip the second line and
reach Audiobookshelf privately at `http://<machine>:13378` instead.

### A3. Audiobookshelf

```bash
python3 tools/bootstrap_abs.py --url http://localhost:13378
```

It creates the admin user and the library (folder `/library`), and prints an
API key once. Add it to the server's `.env` as `ABS_API_TOKEN=<key>`.

### A4. The Mac, the account and the Kobo

On the Mac:

```bash
voxie init          # server: https://<machine>.<tailnet>.ts.net ; paste the ABS key from A3
```

It prints two things for the server: an `UPLOAD_TOKEN=...` line, and a
`docker exec ... gateway_admin.py add-user ...` command. On the server, add
the line to `.env`, restart with `docker compose up -d`, and run the command.
Then on the Mac:

```bash
voxie setup-kobo    # the plugin, Progress sync and the catalogue get the https address
voxie start-ghost   # the watcher
```

In your Audiobookshelf app, use `https://<machine>.<tailnet>.ts.net:8443`.

### A5. Check it from outside

Turn off Wi-Fi on your phone and open
`https://<machine>.<tailnet>.ts.net/health`: it should answer
`{"database": true, "status": "ok"}`. Then read a few pages on the Kobo away
from home and check the position reaches Audiobookshelf (test T7).

**Port 3000 is not needed from the internet**: Funnel reaches the gateway on
the machine itself. On a home network nothing has to be opened on the router.

**Without Tailscale**: Cloudflare Tunnel does the same with a domain you own
on Cloudflare (`cloudflared tunnel`). Its quick tunnels change address every
start, which the Kobo cannot follow, so use a named tunnel. Any reverse proxy
with a real certificate (Caddy, nginx) also works.

---

# On a rented server

The reference host: **Hetzner Cloud CX22** (2 vCPU / 4 GB / 40 GB NVMe, ~€4.5/mo
including the IPv4 address). Nothing here is Hetzner-specific except the
firewall UI — `docker-compose.yml` runs on any VPS.

---

## 0. Before you touch the server

The Mac holds all the state. Copying it while something is writing to SQLite
gives you a torn database, so **stop the local stack first**:

```bash
cd ~/voxie
docker compose down          # closes absdatabase.sqlite and library/database.db cleanly
launchctl unload ~/Library/LaunchAgents/com.librasync.ghost.plist   # stop the watcher
```

Know what you are moving:

| Path        | Size   | Why it matters |
|-------------|--------|----------------|
| `library/`  | ~674 MB| The books, the audio, **and `library/database.db`** — every `sync_node` you have ever generated |
| `config/`   | ~1 MB  | Audiobookshelf's `absdatabase.sqlite`: your ABS user, your API token, your listening progress |
| `metadata/` | ~4 MB  | ABS covers and cached metadata |

Copying `config/` is what makes this a *move* rather than a rebuild — your
existing `ABS_API_TOKEN` keeps working and you don't re-create the ABS account.

---

## 1. Create the server

1. Hetzner Cloud console → new project → **Add Server**
2. **Location**: Falkenstein or Nuremberg (closest to Madrid)
3. **Image**: Ubuntu 24.04 LTS
4. **Type**: Shared vCPU → **CX22**
5. **Networking**: IPv4 + IPv6 (the IPv4 is what the Kobo will use)
6. **SSH key**: add your public key (`cat ~/.ssh/id_ed25519.pub`) — do not use a password
7. Create, and note the IPv4 address.

## 2. Firewall — use Hetzner's, not `ufw`

> **This is the step people get wrong.** Docker writes its own `DOCKER` iptables
> chain that is consulted *before* ufw's rules, so a published container port
> stays reachable no matter what `ufw deny` says. Hetzner's Cloud Firewall runs
> outside the host, where Docker cannot override it.

Cloud Firewalls → Create → apply to the new server. Inbound rules:

| Port    | Source              | Why |
|---------|---------------------|-----|
| 22 TCP  | `0.0.0.0/0`, `::/0` | SSH |
| 3000 TCP| `0.0.0.0/0`, `::/0` | the Kobo pings this from any network it joins |
| 13378 TCP| `0.0.0.0/0`, `::/0`| the Audiobookshelf app |
| ICMP    | `0.0.0.0/0`, `::/0` | ping, for reachability checks |

Leave outbound as allow-all.

Port 22 is deliberately **not** restricted to a single source address. Hetzner
disables password authentication when the server is created with an SSH key, so
the only way in is the private key — and pinning the rule to one IP reliably
locks you out instead, because this project is tested from an iPhone hotspot and
from networks whose address changes. Key-only auth is the control that matters;
verify it with `sshd -T | grep passwordauthentication` (must say `no`).

## 3. Bootstrap the host

```bash
ssh root@<SERVER_IP>

adduser --disabled-password --gecos "" libra
usermod -aG sudo libra
rsync --archive --chown=libra:libra ~/.ssh /home/libra/     # reuse your key

curl -fsSL https://get.docker.com | sh                      # includes the compose plugin
usermod -aG docker libra

# Basic hardening: keys only
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

mkdir -p /opt/voxie && chown libra:libra /opt/voxie
```

Reconnect as `libra` from here on: `ssh libra@<SERVER_IP>`.

## 4. Get the code onto the server

```bash
# on the server, as libra
git clone https://github.com/smukoko/voxie-sync.git /opt/voxie
```

## 5. Write the server's `.env`

`.env` is gitignored, so it does not arrive with the clone. The server needs
**only** the gateway's secrets — leave the Groq, RunPod and OpenRouter keys on
the Mac, where the engine actually runs. Less on a public box is less to lose.

```bash
cat > /opt/voxie/.env <<'EOF'
TZ=Europe/Madrid
PORT=3000

# Same token as the Mac's ~/.librasync.json (step 7 prints it)
UPLOAD_TOKEN=<paste from `voxie init`>

# Comes across with config/ in step 6 — copy the value from the Mac's .env
ABS_API_TOKEN=<paste>
EOF
chmod 600 /opt/voxie/.env
# The key that encrypts each account's own Audiobookshelf token. Written
# straight into the file, never printed. Keep a copy somewhere other than this
# server: without it, stored tokens cannot be read and accounts must set their
# ABS again. It is deliberately not in library/, so a database backup is useless
# without it.
python3 -c "import base64,secrets;print('LIBRA_SECRET_KEY='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" >> /opt/voxie/.env
```

**Check the ABS token is actually live before you trust it.** Tokens accumulate:
a `.env` and a `~/.librasync.json` can each hold one, for different keys, and a
key deleted in the Audiobookshelf UI leaves its token behind looking perfectly
valid. The gateway then reaches ABS and gets a 401, which reads like a routing
fault rather than a stale credential. Decode the token's `keyId` and confirm it
is still in the database:

```bash
python3 -c "import base64,json,sys; p=sys.argv[1].split('.')[1]; print(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4))))" "$ABS_API_TOKEN"
sqlite3 config/abs/absdatabase.sqlite "SELECT id,name,isActive FROM apiKeys;"
```

`ABS_URL` is deliberately absent: `docker-compose.yml` sets it to
`http://audiobookshelf:80`. Inside the container `127.0.0.1` is the *gateway*,
so a host-style ABS URL there silently breaks every progress push.

## 6. Move the data

From the Mac, with the local stack still stopped:

```bash
cd ~/voxie
rsync -avz --progress library/  libra@<SERVER_IP>:/opt/voxie/library/
rsync -avz --progress config/   libra@<SERVER_IP>:/opt/voxie/config/
rsync -avz --progress metadata/ libra@<SERVER_IP>:/opt/voxie/metadata/
```

~680 MB, a few minutes on a normal uplink.

## 7. Start it

```bash
ssh libra@<SERVER_IP>
cd /opt/voxie
docker compose up -d --build
docker compose ps        # librarian_server must reach "(healthy)"
docker compose logs -f librarian
```

A healthy boot prints the banner with **no** `⚠️` lines. `⚠️ UPLOAD_TOKEN is not
set` means uploads are disabled and `.env` was not read.

## 8. Point the Mac and the Kobo at it

```bash
cd ~/voxie
voxie init          # enter the server's IP (or hostname); re-paste ABS + Groq keys
```

`init` prints the `UPLOAD_TOKEN=...` line — it must match the server's `.env`
exactly. It reuses an existing token rather than rotating it, so re-running
`init` later (after an address change) does not break uploads.

```bash
voxie start-ghost   # reload the watcher so it picks up the new address
```

Then **cable the Kobo** and run:

```bash
voxie setup-kobo
```

This is not optional: the address is baked into the device's Lua plugin and its
KOReader kosync `custom_server`. An untouched Kobo keeps pinging the old one.
Eject the device afterwards.

## 9. Verify, in this order

```bash
curl http://<SERVER_IP>:3000/health                    # {"database":true,"status":"ok"}
curl http://<SERVER_IP>:3000/librarian/ping/CLEAR      # {"status":"cleared"}
curl -o /dev/null -w '%{http_code}\n' -F file=@/dev/null http://<SERVER_IP>:3000/upload   # 401 = auth is on
open http://<SERVER_IP>:13378                          # your ABS library, users intact
```

Then the real test: drop an EPUB in `~/Kobo_Dropzone` and watch
`docker compose logs -f librarian` on the server receive the Sync Pack. Finally
open the book on the Kobo and confirm a `[SIDECAR ARRIVED]` line appears.

---

## Operating it

- **Logs**: `docker compose logs -f librarian` (unbuffered, gunicorn access log included)
- **Update**: run the install line again on the Mac, then `voxie init` (press Enter through the questions: it copies the new code to the server and restarts it). By hand: `git pull && docker compose up -d --build`
- **Backup** — everything that matters is three directories:
  ```bash
  docker compose down
  tar czf ~/libra-backup-$(date +%F).tar.gz library config metadata
  docker compose up -d
  ```
  Hetzner snapshots (~€0.01/GB/mo) cover the whole disk and are worth enabling.
- **Disk**: 40 GB holds roughly a hundred audiobooks. `df -h` occasionally.
- **Ghost items in ABS** (a book whose folder is gone): deleting through the
  gateway removes the ABS item too; anything older is found from the disk with
  `docker exec librarian_server python tools/gateway_admin.py abs-cleanup`
  (add `--apply` after reading the list; it never deletes files).

- **Accounts with their own Audiobookshelf.** An account sets it from its Mac
  (`voxie set-abs`, which needs the gateway behind HTTPS and `GATEWAY_URL`
  in `~/.librasync.json`), or the admin does:
  `docker exec -it librarian_server python tools/gateway_admin.py set-abs alice --url https://abs.alice.example`
  (asks for the token, hidden). Only public addresses are accepted for anyone
  but the owner. `list` shows each account's ABS and whether its token is
  encrypted. To rotate the key: put the new key first,
  `LIBRA_SECRET_KEY=<new>,<old>`, recreate the container, run `rotate-abs-key`,
  then drop the old key.

### Once: one ABS item per book (2026-09-28)

Books used to live in `library/ebooks/<title>/` *and* `library/audiobooks/<title>/`,
so ABS listed each narrated book twice. The ebook now sits beside its audio,
in the folder ABS already knew, so item ids, listening progress and chapters are
kept. After deploying the gateway that carries this change:

```bash
docker exec librarian_server python tools/gateway_admin.py migrate-layout           # read the list
docker exec librarian_server python tools/gateway_admin.py migrate-layout --apply
docker exec librarian_server python tools/gateway_admin.py abs-cleanup              # the duplicates + ghosts
docker exec librarian_server python tools/gateway_admin.py abs-cleanup --apply
```

Then in ABS, a library whose only folder is `/library/ebooks` (if there is one)
is empty and can be removed in Settings → Libraries.

## Known limits

- **HTTPS needs something in front of the gateway.** The gateway itself
  speaks plain HTTP on port 3000; the compose file's Caddy (see the top of
  this page; `voxie init` switches it on when you give a domain), Tailscale
  Funnel or another TLS proxy goes in front. Changing the address means
  re-pairing the Kobo. The plugin (1.5) and the installer use whatever address you give
  (`core/gateway_address.py`); verified on the real KOReader in kosim
  (2026-10-03). The plugin checks the certificate against KOReader's own CA
  bundle, so use a real certificate (Funnel and Caddy provide one). KOReader's
  built-in Progress sync encrypts too but checks no certificate: that is
  KOReader's choice, not ours.
- **Registration is closed by default.** `/users/create` answers 403 unless
  `ALLOW_REGISTRATION` is set; `tools/gateway_admin.py add-user` makes accounts.
- **A bare IP gets baked into the Kobo.** Point a domain at the server and use
  that as the address in `voxie init`; then the device survives a change of
  host without being re-cabled.
