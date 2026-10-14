# The Voxie server

Your Kobo and your phone sync through a small server of your own: the Voxie
gateway and Audiobookshelf, in Docker. **`voxie init` sets it up for you.**

## The quick way (recommended)

1. **Create a server** at [Hetzner](https://www.hetzner.com/cloud): the
   smallest cloud server, **Ubuntu 24.04**, about €5 a month. When it asks for
   an SSH key, paste yours: `cat ~/.ssh/id_ed25519.pub` (no key yet?
   `ssh-keygen -t ed25519` first).
2. **Optional, for HTTPS:** a domain name (about €10 a year at any
   registrar). Add an **A record** pointing it, e.g. `voxie.example.com`, at
   the server's IP address.
3. On your Mac: **`voxie init`**, choose *Set one up for me over SSH*, and give
   it the server's IP (and the domain, if you have one).

That is all. `init` copies Voxie to the server, installs Docker, starts the
gateway and Audiobookshelf, creates your account, and, with a domain, gets a
free Let's Encrypt certificate. Nothing is typed on the server.

**Updating:** run the install line again, then `voxie init` (press Enter
through the questions). When the update changes the Kobo's part, plug the
Kobo in and run `voxie setup-kobo` too.

**Any other Linux machine works the same** if you can SSH into it as root with
a key: another provider's VPS, a mini PC or a Raspberry Pi. At home it also
needs to be reachable from outside; see the reference below.

## Without a domain

The Kobo then talks to `http://<ip>:3000`: it works, but your login and reading
positions travel unencrypted (avoid public Wi-Fi), and a new server address
means plugging the Kobo in again. You can add a domain later: run `voxie init`
again, then plug the Kobo in once more.

## More

[docs/SERVER_REFERENCE.md](docs/SERVER_REFERENCE.md): everything by hand, a
server at home behind Tailscale Funnel, firewall, backups, operating it.
