# IPv6 hook release checklist

All eleven source projects use `feature/ipv6-change-hooks`. Merge and release them
as one coordinated change: the event-driven daemon replaces the old full-iservchk
update path, and installed consumers must provide their package-owned hooks.
The versions below are unreleased changelog versions, not published releases.

## Required packages

- [ ] `stsbl-ipv6chdetectd`: release `26.10.28.1` or a newer version; binaries: `ipv6chdetectd`.
- [ ] `stsbl-iserv-config-ipv6`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-config-ipv6`.
- [ ] `stsbl-iserv-dns-over-tls`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-dns-over-tls`, `stsbl-iserv-dns-over-tls-tunnel`.
- [ ] `stsbl-iserv-dns-ula`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-dns-ula`.
- [ ] `stsbl-iserv-ipv6-support`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-ipv6-support`.
- [ ] `stsbl-iserv-radius-based-vlan`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-radius-based-vlan`.
- [ ] `stsbl-iserv-server-all-knowing-dns`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-server-all-knowing-dns`.
- [ ] `stsbl-iserv-server-dhcpy6d`: release `26.10.29.3` or a newer version; binaries: `stsbl-iserv-server-dhcpy6d`.
- [ ] `stsbl-iserv-server-radvd`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-server-radvd`.
- [ ] `stsbl-iserv-server-tayga`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-server-tayga`.
- [ ] `stsbl-iserv-vpn-ipv6`: release `26.10.28.1` or a newer version; binaries: `stsbl-iserv-vpn-ipv6`.

## Release gates and order

- [ ] Publish radvd/radvdump `1:2.20-1~stsblbpo12+1` (or compatible newer packages)
  for supported systems before releasing dns-ula.
  The static `autoignoreprefixes` exclusion is unsupported by 2.19. dns-ula declares
  a versioned Breaks; do not bypass it or add the syntax to a running 2.19 system.
- [ ] Review and merge all eleven feature branches, including daemon logging,
  BIND dated-backup protection, and DNS anycast prefix/route exclusions.
- [ ] Finalize changelogs and choose versions above all already published versions.
- [ ] Build and publish consumer packages (including dns-over-tls-tunnel) and the
  compatible radvd package before or together with ipv6chdetectd. Check the
  daemon's versioned Breaks and consumers' Depends against the final versions.
- [ ] Verify upgrade dependency resolution on base-only and portal installations.
- [ ] Run daemon regression tests and package checks for the final release heads.
- [ ] Verify DHCP client, dhcpcd, ifupdown and kernel notifications, debounce,
  events during a hook pass, failure retries and recovery after daemon restart.
- [ ] Verify unchanged configuration causes no writes or service actions; changed
  configurations are validated and reloaded/restarted by their owning hooks.
- [ ] Verify DNS RDNSS stays advertised, fd00::53/128 is absent from autonomous
  prefixes and default-interface routes, and wildcard prefix discovery remains.
- [ ] Verify historical BIND zone backups are untouched and stopped optional
  services stay stopped. Apache and user homepages remain outside this change.
- [ ] Upgrade a test system using the actual release packages; source installation
  alone does not prove release-package dependencies or upgrade behavior.

## Development VM verification, 2026-11-02

Source copies are under `/var/local/src/iserv` on `felix.iserv.dev`;
`ISERV_SRC_DIR` is unset in noninteractive SSH sessions.

All eleven source copies were pulled with `--ff-only` on their existing feature
branches, and all eleven `iservmake iservinstall` runs succeeded. dns-ula includes
signed commit `05657ee`. The daemon regression suite passed all 31 tests.

The VM's APT repositories only offered radvd 2.19. A Bookworm backport was built
with `cowbuilder-dist bookworm build`, using authenticated Debian 2.20-1 sources.
The resulting radvd and radvdump `1:2.20-1~stsblbpo12+1` packages are installed.
The single backport changelog entry precedes the unchanged Debian history.
The `.dsc`, source and binary `.changes`, and build metadata are signed; all
referenced SHA256 checksums and signatures were verified. Debian's packaging
sets `DEB_BUILD_OPTIONS=nocheck`; the backport build does not prove an upstream
unit-test run. The resulting binary passed a configuration parser check with
`autoignoreprefixes` and the wildcard prefix block.

Artifacts, including the complete source package, are stored locally in
`~/git/stsbl/radvd-backport/` and on the VM in `/var/tmp/radvd-stsblbpo12/`.
Publish radvd and radvdump together with the `.dsc`, original tarball/signature,
Debian tarball and signed source/binary upload metadata. This backport has not
been uploaded to a package repository. The production router was not changed.

The initial audit found no open code changes. Existing radvd POT header churn
and an untracked generated Makefile in all-knowing-dns are build artifacts and
are preserved rather than included as release changes.

The VM had an iconf patch commenting out the DNS route. The new filter makes it
redundant, and keeping it caused a patch conflict. The patch and previous config
were archived in `/var/tmp/ipv6-radvd-migration-20261102/`. The hook worker then
regenerated `/etc/radvd.conf`, validated it and reloaded radvd successfully:
52 targets checked, one updated, two actions applied, zero deferred consumers.
Both LAN blocks retain RDNSS and wildcard prefixes, with the anycast exclusion;
no active DNS route block remains. Pending application state was empty, both
services were active, and `dpkg --audit` was clean. Upstream network events still
queue further passes; no idle-queue claim is made.
