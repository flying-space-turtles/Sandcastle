#!/usr/bin/env bash
# Default Sandcastle firewall ruleset.
# Edit this file to customise traffic filtering between teams.
set -euo pipefail

echo "[firewall] Applying iptables rules..."

# Flush existing rules
iptables -F
iptables -t nat -F
iptables -t mangle -F
iptables -X 2>/dev/null || true

# Default policies — permissive by default so CTF connectivity works out of
# the box.  Switch FORWARD to DROP and add explicit ACCEPT rules below to
# lock things down.
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
iptables -P FORWARD ACCEPT

# Fast-path: accept packets belonging to established connections.
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Log every NEW forwarded connection (rate-limited to avoid log floods).
iptables -A FORWARD -m conntrack --ctstate NEW -m limit --limit 50/sec --limit-burst 100 \
    -j LOG --log-prefix "[SANDCASTLE-FW] " --log-level info

# ── Example rules (uncomment to use) ─────────────────────────────────────────
#
# Block team1 from reaching team2's vuln service:
#   iptables -A FORWARD -s 10.10.1.0/24 -d 10.10.2.3 -p tcp --dport 8080 -j DROP
#
# Only allow specific service ports between teams, drop everything else:
#   iptables -A FORWARD -p tcp --dport 8080 -j ACCEPT
#   iptables -A FORWARD -p tcp --dport 22 -j ACCEPT
#   iptables -A FORWARD -j DROP
# ──────────────────────────────────────────────────────────────────────────────

echo "[firewall] Rules applied. Forwarding enabled."
echo "[firewall] Monitoring traffic..."

# Keep the container running.
exec tail -f /dev/null
