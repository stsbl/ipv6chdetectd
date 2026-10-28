# Event-driven IPv6 configuration updates

## Intent and scope

Replace ipv6chdetectd's periodic netquery6 snapshots and full iservchk runs with asynchronous event notifications and package-owned update hooks. Work exclusively in ~/git/stsbl; ~/git/iserv stays untouched.

The user requires isc-dhclient, dhcpcd and ifupdown notifications, and confirmed that kernel events must cover SLAAC and manual address changes too. Existing netquery6 queries remain available to configuration generators; they are removed from change detection. The user additionally requires LAN-dependent generated configurations from both official IServ and STSBL projects. Official repositories are read-only reference material. Apache and user homepages are explicitly excluded by the user as dead functionality, including legacy iserv2-* Apache configuration and stsbl-iserv-homepage.

## Event processing

Keep the ipv6chdetectd package and service name. Replace the forking polling process with a foreground worker supervised by systemd. Subscribe to Linux routing netlink link, IPv6 address and IPv6 route notifications. Filter unrelated families and ignore address lifetime refreshes that do not change usable configuration. React to address removal, link loss and removal of the last IPv6 address.

Provide an ipv6chdetectd notification command for installed isc-dhclient exit hooks, dhcpcd hooks and ifupdown post-up/post-down scripts. It records a pending event and wakes the worker without waiting for configuration generation or service reloads. Validate root ownership, notification inputs and runtime directory permissions. Hooks must be safe when the worker is unavailable; pending work survives worker restarts. Use a persistent queue marker if necessary to cover reboot, plus an unconditional reconciliation on worker startup.

The user explicitly requires a debouncing event queue. Collect events for a short fixed debounce window (builder default: three seconds from the first queued event), then execute one hook pass for the entire collected batch. Atomically separate that batch from the pending queue before running hooks. Continue collecting incoming events while hooks execute; after the pass, execute one subsequent pass for all events collected during it, waiting only for any remaining part of their debounce window. Do not execute one pass per event, extend the window indefinitely, clear events that arrived during execution, or overlap hook passes. Repeat this batching rule for events arriving during each subsequent pass. Maintain event ingestion independently of potentially slow hook execution. A hook failure is logged with its identity and exit status, does not suppress other independent consumers, and remains pending for bounded-backoff retries. Never report scheduling as successful application. No periodic netquery6 comparison, WIDE reload, full iservchk or synchronous network-client reconfiguration.

## Consumer hook interface

Packages install ordered executable scripts under /usr/lib/iserv/ipv6chdetectd.d. Hooks run as root with a fixed PATH and stable working directory. They reconcile current state rather than relying on notification order or trusting event-provided shell commands. A package-supplied helper regenerates explicit iconf targets, detects real content changes, and applies each affected service once per pass. Failed generation must not trigger a reload of incomplete configuration. Account for iconf return codes, parameterized DNS targets and existing iconf apply behavior before choosing helper details.

Run preparation before configuration generation, then application. Refresh dynamic LAN/external-IP configuration before dependent templates. Regenerate shared files once even when multiple packages contribute fragments. Regenerate host-derived data once where required by newhosts consumers. Hooks must reload or restart daemons that read changed generated files or changed IServ configuration, including cached LAN values. Prefer a supported reload only when it applies every affected setting; otherwise restart. Do not repeatedly restart services for an unchanged effective configuration. A failed reload/restart is a failed application and must remain eligible for retry even if the generated file already matches on the next pass. Avoid activating intentionally disabled services, but preserve radvd/dhcpy6d's existing enable/stop decisions when IPv6 appears or disappears.

## Repository coverage

- stsbl-ipv6chdetectd: worker, notification CLI, event-source adapters, shared hook helper, systemd migration and queue lifecycle. Remove obsolete PID/state handling and dependencies where unused.
- stsbl-iserv-config-ipv6: sysctl/address-privacy configuration and IPv6 DNS update behavior. Do not restart dhcpcd in response to its own address events.
- stsbl-iserv-ipv6-support: LAN/external-IP synchronization, ULA prerequisites, generated host data, BIND forward/reverse/ACL data, ferm, squid/squid-block, Samba and DHCPv6 PXE fragments.
- stsbl-iserv-server-radvd: radvd configuration and lifecycle; shared ferm targets.
- stsbl-iserv-server-dhcpy6d: DHCPv6 configuration and interface lifecycle. Coordinate with existing delegated-prefix hooks so a prefix change does not cause redundant restarts.
- stsbl-iserv-dns-ula: ULA prerequisites and contributions to radvd/dhcpy6d. Only change interfaces when required; avoid event feedback loops.
- stsbl-iserv-dns-over-tls: BIND ACL and stunnel configuration, including its alternate tunnel template tree.
- stsbl-iserv-radius-based-vlan: FreeRADIUS listen configuration.
- stsbl-iserv-server-all-knowing-dns: DNS listener configuration.
- stsbl-iserv-server-tayga: squid and shared firewall contributions; audit its LAN-dependent BIND DNS64 exclusions (currently IPv4-only). Install the hook in this package itself.
- stsbl-iserv-vpn-ipv6: package-owned hook reconciles active PPP radvd fragments under /var/lib/iserv/vpn-ipv6/radvd. These persist after startup and embed LAN routes and netquery6-derived RDNSS addresses. Regenerate active-session advertisements after changes without re-running the up action, adding addresses again, disconnecting sessions, or activating inactive PPP interfaces. Apply each changed radvd-ppp instance and coordinate the main radvd reload.
- stsbl-backend-server: update-reverse-dns is an explicit remote administrative command; do not invent a remote destination or automatically invoke SSH. Document this consumer classification.

Interactive query tools remain interactive. Every persisted address-dependent configuration must have an update path; shared targets are coordinated centrally, with package declarations instead of a hard-coded list of installed optional services in the daemon. Existing iservchk provisioning remains usable for installation and administrative checks.

## LAN configuration consumer audit and ownership

Read-only searches of tracked files across ~/git/iserv and ~/git/stsbl found the following generated configuration consumers. Official package compatibility hooks are shipped by STSBL, never added to official repositories. Base-system-installable package handling belongs in stsbl-iserv-config-ipv6; portal-dependent package handling belongs in stsbl-iserv-ipv6-support. Classify by actual binary package dependencies, including transitive dependencies, rather than repository name or appearance on portal installations.

Each optional official consumer must be guarded by `iservpkginstalled <binary-package>` before registering or executing its updates. For example, `iservpkginstalled iserv-server-bind` gates the base BIND update, and `iservpkginstalled iserv-wlan` gates WLAN FreeRADIUS client configuration. A missing package is a successful skip. File existence alone does not replace the installation check. Packages whose outputs share a target contribute to one common regeneration/application action. Avoid duplicate ownership when both compatibility packages are installed.

### Official consumers handled by stsbl-iserv-config-ipv6

| Installed package guard | Generated target or state | Application |
| --- | --- | --- |
| iserv-update | /etc/ssh/sshd_config, including LAN-based root-password exception | Validate sshd configuration and reload the running SSH service if changed |
| iserv-server-bind | /etc/bind/named.conf.iserv, fallback view match-clients | Coordinate with other BIND targets, validate, then apply once |
| iserv-server-exim | /etc/exim4/exim4.conf, lan_hosts | Validate and apply Exim configuration if changed; the LAN hostlist is contributed by the main package, while the base-only package does not contribute it; avoid requiring the portal relay variant |
| iserv-server-spamassassin | /etc/spamassassin/iserv.cf, LAN welcomelist rules | Apply to spamd or legacy spamassassin according to the installed release/service |
| iserv-server-openldap and iserv-server-ferm | /etc/ferm.d/20iserv.conf, OpenLDAP LAN access | Shared firewall regeneration/application |
| iserv-server-docker and iserv-server-ferm | /etc/ferm.d/99defaults.conf, Docker LAN restrictions | Audit existing IPv4-only domain; include through shared firewall update without restarting Docker |
| putty-iserv plus iserv-server-nginx | /etc/nginx/server-iserv, download access rules | Shared nginx validation/reload; putty-iserv itself has no portal dependency |

### Official consumers handled by stsbl-iserv-ipv6-support

| Installed package guard | Generated target or state | Application |
| --- | --- | --- |
| iserv-portal | /etc/ferm.d/20iserv.conf and LAN ipsets declared by share/server-ferm/ipsets.d/iserv-portal-lan.yml | Refresh ipsets through the supported chkipsets repair path and coordinate one firewall application |
| iserv-portal | /etc/nginx/server-iserv and /etc/nginx/server-http-iserv, including DoH and download access restrictions | Shared nginx validation/reload |
| iserv-deploy, iserv-deploy-linux, iserv-pxe, iserv-anydesk | Contributions to nginx server-iserv, server-http-iserv and sites-available/iserv | Register applicable targets per installed contributor; one nginx application |
| iserv-portal | /etc/squid/squid.conf, LAN destination ACLs | Shared squid application |
| iserv-wlan | /etc/freeradius/3.0/clients.conf | Validate and apply FreeRADIUS once together with STSBL RADIUS contributions |
| iserv-docker-matrix | /etc/iserv/docker-matrix/homeserver.yaml, URL-preview LAN blacklist | Validate and restart only the running Matrix service/container through its existing lifecycle, without running provisioning or image-import steps |

BIND portal reverse-zone generation (/etc/bind/includes/view.common-zones.conf and chkdnszones) and /var/lib/iserv/config/proxy.pac reference LAN but explicitly filter IPv6. Record these as audited IPv4-only consumers: IPv6 additions/removals alone do not justify a reload or expanding their IPv6 functionality. Existing STSBL BIND address/ACL hooks still apply their actual IPv6-dependent targets. Likewise, do not restart Docker merely because an IPv4-only firewall fragment mentions LAN.

Nginx target paths vary with installed package versions. Register the supported current targets from installed iconf schemas, preserving overrides; never recreate removed legacy sites just because an old checkout contains a template. Independently guard the service owner as well as the contributing package. Base and portal contributions to shared targets must be deduplicated in the common hook helper.

### STSBL package-owned hooks

The user explicitly permits each STSBL package to install its own hook under /usr/lib/iserv/ipv6chdetectd.d. Keep these hooks in their respective source packages rather than moving them into compatibility packages.

- stsbl-iserv-ipv6-support owns dynamic LAN synchronization and squid-block LAN ACLs, alongside its existing netquery6-derived targets. Synchronize LAN before every downstream consumer reads the configuration. Preserve UpdateIPv6LAN and UpdateIPv6Regdns administrator choices. The old five-minute LAN synchronization cron must not race with the event-driven path: retire it or share serialization and notifications, including notifying consumers when LAN changes outside a daemon pass.
- stsbl-iserv-server-tayga owns its BIND DNS64 and firewall/squid contributions. Its BIND LAN exclusion loop currently ignores IPv6; classify it accordingly and suppress unchanged applications.
- stsbl-iserv-vpn-ipv6 owns active PPP advertisement reconciliation described above.
- Other STSBL packages listed in repository coverage own their existing address-dependent configuration hooks.
- stsbl-iserv-homepage and all Apache/homepage compatibility paths are excluded, per the user's explicit instruction.

The user explicitly requires hooks to reload or restart daemons that read the changed configuration. Regenerating a file or running newconf alone is insufficient when a running process retains the old values. Audit both generated-file readers and long-running readers of IServ configuration, including cached LAN values. Each hook must request the appropriate daemon reload when it re-reads all affected values; otherwise request a restart. Coordinate shared daemon actions once per pass and preserve intentionally stopped services. Request/CLI readers that load the current configuration on each invocation need no daemon action, but verify that behavior rather than assuming it from a source reference. Runtime authentication/access checks and HostBundle network checks must therefore be traced to their actual process/cache lifetime and covered by the responsible daemon action where needed. Comments, translations, tests, Wake-on-LAN prose and archived hg/hg-raw snapshots are not configuration consumers.

## Packaging and compatibility

Use each repository's Debian packaging and changelog conventions. Ensure hooks and helpers are shipped executable, tests are excluded, and package dependencies require a version that provides the hook API. Avoid circular dependencies between ipv6chdetectd and config-ipv6. Preserve administrator configuration overrides and existing unrelated working-tree changes. No commits, pushes or external publication are required by this request.

## Validation

Use isolated fake netquery6, iconf, service and network clients to test real worker/adapter behavior without modifying host networking. Cover event bursts producing exactly one pass, multiple events during a blocked hook producing exactly one subsequent pass, events during that subsequent pass producing one further batch, fixed-window timing, queueing during execution, worker restart, consumer failure/retry, last-address removal, netlink IPv6 filtering, source hooks returning promptly, unchanged-file reload suppression, shared-target deduplication and ordering. Run affected existing template tests and packaging/syntax checks. Audit every tracked netquery6 occurrence across STSBL repositories and LAN-dependent generated configurations in both workspaces against the final consumer map. Test absent-package skips with mocked iservpkginstalled, base-only installations without iserv-portal, combined base/portal shared-target deduplication, disabled automatic LAN synchronization, LAN preparation before downstream generation, and active PPP advertisement updates. Test that daemon readers receive the required reload/restart after effective configuration changes, including cached LAN changes with unchanged generated files; a failed application must retry after the file was successfully written. Assert that no Apache/homepage actions are registered. Report any live-system verification that cannot be performed locally.

## Implementation decisions delegated to the builder

Choose the smallest maintainable runtime and netlink binding available in the target Debian packages, after checking dependency availability. Set concrete debounce/backoff limits and document them. Inspect actual service application semantics before implementing each hook. Retain startup reconciliation; do not add a snapshot polling fallback.
