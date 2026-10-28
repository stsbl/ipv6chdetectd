# IPv6 change hooks implementation plan

**Goal:** Apply IPv6 and LAN changes through debounced package hooks.
**Architecture:** Foreground Python netlink/select loop with durable notifications, a separate serialized hook process, and persistent service application retries. Shell package hooks register shared configuration targets and daemon actions.
**Tech stack:** Python standard library, POSIX shell/Bash, Debian, iconf and systemd.
**Spec:** ../specs/2026-10-28-ipv6-change-hooks-design.md

## Constraints
Work in STSBL only; official IServ sources are read-only. No Apache/homepages. Hook directory /usr/lib/iserv/ipv6chdetectd.d. Debounce three seconds; collect during execution. Official compatibility hooks use iservpkginstalled and base/portal ownership. No full iservchk, polling snapshots or WIDE restarts. No commits/pushes requested.

## Review focus
Lost events across queue consumption/restart; noisy netlink lifetimes; failure after successful file writes; shared service validation/application; running PPP sessions and last-address loss.

## Tasks
- [x] 1. tests/test_events.py: behavioral tests for DebounceQueue, netlink filtering, real notification/worker batching and restart. Implement sbin/ipv6chdetectd and usr/lib/iserv/ipv6chdetectd/{events.py,daemon.py}; Python stdlib only. Run unittest discovery.
- [x] 2. tests/test_hooks.py: fake iconf/systemctl executable integration tests for deduplication, guards, generation errors, unchanged output, service validation and durable apply retries. Implement hooks.py and shell functions. Run unittest discovery.
- [x] 3. Source adapters and systemd/packaging: add dhclient/dhcpcd/ifupdown notifications, foreground daemon migration, dependency floors and test exclusions. Test source scripts without network mutation.
- [x] 4. Package-owned and official compatibility hooks: prepare LAN/ULA/host data before registration, apply shared configs with guarded service actions, eliminate cron races, refresh live PPP advertisements. Add meaningful consumer tests and run existing tests.
- [x] 5. Audit consumers, documentation/changelogs, syntax/build checks and fresh whole-change review; fix verified defects and record limitations.

## Progress and rulings
Execution is inline under the user's Implement instruction. Existing checkouts are clean except the approved design; use feature branches in place to keep requested paths reviewable. User authorization covers implementation without another plan approval round.

Implementation review found and regression tests verified fixes for post-application hash races, independent preparation failures, missing ULA interface generation, empty ExternalIPs reacquisition, query-error deletion, prefix expiration and timeout descendants. Preparation is split into ULA, LAN and host-data hooks with explicit dependencies. Package staging caught unsupported exclusions in legacy .install files; affected rules were converted to .iservinstall. Existing official repositories were used read-only. No commits or live deployment were performed.

Final verification: iservmake run_tools passed in all 11 affected repositories. Test counts: daemon/hook suite 26; config-ipv6 129; ipv6-support 17; server-dhcpy6d 24; VPN 1 (197 total), plus route-hooks.sh. dh_iservinstall staging verified hooks/adapters and test exclusions in all 11 source repositories, including the split TLS-tunnel binary. Full modified/new-file whitespace checks passed using temporary Git indexes; systemd-analyze verify passed with the source executable substituted for the intentionally uninstalled target. Final independent review confirmed upstream ownership, downstream preservation and legacy-prefix expiry behavior. Real DHCP/SLAAC transitions and production daemon reloads were not run on this workstation.
