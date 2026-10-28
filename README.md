# IPv6 configuration change hooks

`ipv6chdetectd` listens for IPv6 address/route and link events over routing netlink. isc-dhclient, dhcpcd and ifupdown also call `ipv6chdetectd --notify` after configuration transitions. It does not poll netquery6 or run a full iservchk.

The worker collects events for three seconds from the first event in a batch. Hook passes never overlap. Events arriving during a pass form the next batch; when their window has elapsed, one follow-up pass handles everything collected. Startup reconciles current state. Pending/in-flight batches and failed daemon applications survive restarts in `/var/lib/iserv/ipv6chdetectd`, owned by root with mode 0700. Retries back off from three to sixty seconds. Source hooks return without waiting for configuration generation or daemon actions.

Address/route lifetime refreshes, cloned routes and temporary privacy addresses are filtered. This avoids repeated reconciliation from countdown changes. A notification from a client with an older read-only service namespace can be persisted by the running worker through its Unix socket; the package also installs write-path drop-ins for dhcpcd and networking.

## Package interface

Install an executable, run-parts-compatible name under `/usr/lib/iserv/ipv6chdetectd.d`. The worker invokes every hook in lexical order in two phases, using a fixed PATH and `/` as its working directory:

- `IPV6CHDETECTD_PHASE=prepare`: reconcile prerequisites such as ULA addresses, LAN configuration and host-derived files.
- `IPV6CHDETECTD_PHASE=configure`: declare generated files, validators and daemon actions through `/usr/lib/iserv/ipv6chdetectd-functions`.

Example:

```sh
#!/bin/sh
set -e
. /usr/lib/iserv/ipv6chdetectd-functions
[ "$IPV6CHDETECTD_PHASE" = configure ] || exit 0
if iservpkginstalled iserv-server-bind; then
  ipv6_config bind9.service reload /etc/bind/named.conf.iserv
  ipv6_validate bind9.service named-checkconf
fi
```

Declarations:

| Function | Arguments | Meaning |
| --- | --- | --- |
| `ipv6_config` | service reload\|restart targets... | Regenerate installed iconf targets and apply only effective changes |
| `ipv6_watch` | service reload\|restart files... | Apply when externally generated inputs change, including cached IServ config |
| `ipv6_validate` | service command arguments... | Validate the complete service configuration before application |
| `ipv6_action` | service command arguments... | Override the default systemctl reload/restart action |
| `ipv6_state` | service start\|stop | Start an enabled service when needed, or stop it on address loss |
| `ipv6_remove` | service reload\|restart files... | Remove obsolete generated configuration |
| `ipv6_requires` | service preparation-hook-names... | Block this consumer when a required preparation failed |
| `ipv6_always` | logical-service command arguments... | Run a self-reconciling operation on each pass, with retry on failure |
| `ipv6_query` | netquery6 arguments... | Treat status 1 as valid empty output and reject actual query errors |
| `ipv6_has_addresses` | none | Test usable LAN IPv6 addresses; errors abort the hook rather than entering its address-loss branch |

A service name starting with `@` identifies an operation without a systemd unit. It must supply an explicit action. Ordinary daemon actions preserve intentionally stopped services; explicit `start` policy starts only enabled units. A supported reload must re-read every changed setting; use restart otherwise. Shared targets, validators and daemon actions are deduplicated across packages; restart takes precedence over reload. Generation failures block affected services, while independent consumers continue. Preparation failures block declared dependants and the failing hook's own consumers. Safe shutdown on address loss remains possible after a preparation failure.

The runner persists application intent before writing configurations. It validates after all files have been generated and records the exact pre-action input snapshot only after successful application. A failed reload/restart remains pending even when the files already contain the desired data. A later change during application is detected in the next batch. Commands have a five-minute limit, and timeout/shutdown terminates their helper process groups before another pass can run.

## Consumers and ownership

Official IServ checkouts stay unchanged. `stsbl-iserv-config-ipv6` supplies guarded compatibility hooks for base-system packages: SSH, BIND, Exim, SpamAssassin, ferm, PuTTY nginx rules and PHP-FPM configuration readers. `stsbl-iserv-ipv6-support` supplies portal compatibility for nginx, Squid/squid-block, BIND address data, WLAN FreeRADIUS clients, Matrix and Samba. Its preparation hooks synchronize LAN and external-IP settings and host data before dependent generation; automatic-update settings remain respected. PHP-FPM's existing reload propagation applies generated PHP configuration to the running versioned units.

STSBL DNS, DHCPv6, radvd, VLAN/RADIUS, TAYGA and VPN packages install their own hooks. Active PPP advertisements retain their assigned session prefixes while refreshing LAN routes and RDNSS; their running radvd instances are reloaded. The managed dhcpy6d client hook writes its delegated-prefix environment and queues the change, allowing one restart after the configuration settles. It records the owning upstream interface, preserves its prefix on downstream events and clears it on upstream loss. Legacy environment files infer ownership from the configured IA_PD stanza before the first renewal. The ULA interfaces file is regenerated for future ifupdown operations; preparation hooks reconcile managed runtime ULA addresses without bouncing physical interfaces.

Apache and user-homepage configurations are excluded. Explicit remote administrative commands such as `stsbl-backend-server/sbin/update-reverse-dns` remain explicit because their destination cannot be inferred. IPv4-only reverse-zone, proxy.pac and TAYGA/Docker fragments are audited; unchanged effective output does not trigger service application. No hook automatically runs the remote backend command.

## Validation

Run `python3 -m unittest discover -s tests -v`. Consumer-script integration tests additionally use the sibling STSBL source checkouts and skip when those optional checkouts are absent. They exercise real hook subprocesses with temporary fixtures, without modifying host networking or services. The integration test's Unix sockets must be permitted by the test environment.

Also run the existing Perl suites in config-ipv6 and server-dhcpy6d, the LAN/DNS persistence suite in ipv6-support, and the PPP refresh Python suite in vpn-ipv6. Stage each package with dh_iservinstall to verify the new hooks and source adapters are shipped and tests are excluded. Live DHCP/SLAAC transitions and real daemon configuration validators require a representative IServ test system.
