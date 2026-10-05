#!/bin/bash
# One-time root setup for the read-only Scalable Capital connection.
#
# Run once on the app LXC, as root, from the deployed checkout:
#
#   bash /opt/quantfolio/infra/scalable/install.sh
#
# It downloads the official `sc` release, verifies Scalable's minisign
# signature and the checksum, installs it with the read-only wrapper, the
# sudoers rule and the CLI config (`allowed_isins = []`), and checks that the
# Quantfolio service user can reach the wrapper. Everything after that (the
# login, pinning the binary, turning sync on) happens in the Control Center.
# Safe to run again: it reinstalls the same files and keeps the login.
#
# sc needs glibc >= 2.38. On an older system (Debian 12 has 2.36) it also
# fetches Debian 13's glibc with apt (checked against Debian's archive
# signature) into /usr/local/lib/quantfolio-sc-runtime, which only the wrapper
# uses to start sc; the rest of the host keeps its own glibc.
#
# Options (environment): SC_TAG=vX.Y.Z to pin a release (default: latest),
# APP_USER (default quantfolio).
set -euo pipefail
umask 022

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO="ScalableCapital/scalable-cli"
# Scalable Capital's release signing key, as published in the sc README.
readonly MINISIGN_PUBLIC_KEY="RWRKuuSASIzbSYpuU5gdXeTkXirJBl5+XVXLP6E60hBUUKZ5HPIGjV8b"
readonly APP_USER="${APP_USER:-quantfolio}"
readonly CLI_USER=scalable-cli-user
readonly CLI_HOME=/var/lib/scalable-cli
readonly SC=/usr/local/bin/sc
readonly WRAPPER=/usr/local/libexec/quantfolio-sc-ro
readonly SUDOERS=/etc/sudoers.d/quantfolio-sc
readonly RUNTIME=/usr/local/lib/quantfolio-sc-runtime

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (e.g. from the Proxmox console of the app LXC)"
id "$APP_USER" >/dev/null 2>&1 || die "service user '$APP_USER' does not exist; set APP_USER"
for f in quantfolio-sc-ro sudoers.quantfolio-sc config.toml; do
  [[ -f "$HERE/$f" ]] || die "$HERE/$f is missing; run this from the Quantfolio checkout"
done

say "Installing tools (curl, minisign, sudo, flock)"
missing=()
for pkg in curl minisign sudo util-linux ca-certificates python3; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if ((${#missing[@]})); then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi

case "$(uname -m)" in
  x86_64) arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac

tag="${SC_TAG:-}"
if [[ -z "$tag" ]]; then
  tag="$(curl -fsSI "https://github.com/$REPO/releases/latest" \
    | awk 'tolower($1) == "location:" {print $2}' | tr -d '\r' | awk -F/ '{print $NF}')"
fi
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]] || die "could not determine the sc release tag (got '$tag'); set SC_TAG"

say "Downloading sc $tag ($arch) and verifying Scalable's signature"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
asset="sc-${tag}-linux-${arch}-gnu.tar.gz"
sums="sc-${tag}-SHA256SUMS"
base="https://github.com/$REPO/releases/download/$tag"
for f in "$asset" "$sums" "$sums.minisig"; do
  curl -fsSL --retry 3 -o "$work/$f" "$base/$f" || die "download failed: $base/$f"
done
minisign -Vq -P "$MINISIGN_PUBLIC_KEY" -m "$work/$sums" -x "$work/$sums.minisig" \
  || die "the checksum file is NOT signed by Scalable Capital; nothing was installed"
(cd "$work" && grep -F "  ${asset}" "$sums" | sha256sum -c --status -) \
  || die "the download does not match the signed checksum; nothing was installed"
mkdir "$work/x"
tar -xzf "$work/$asset" -C "$work/x"
binary="$(find "$work/x" -type f -name sc -perm -u+x | head -n 1)"
[[ -n "$binary" ]] || die "no sc binary inside $asset"
install -o root -g root -m 0755 "$binary" "$SC"

# Hold the wrapper's session lock (quantfolio-sc-ro, lock_session) while
# $RUNTIME is replaced or removed: the wrapper picks how to start sc only under
# that lock, so no sync or login starts halfway through. A running sc call keeps
# the lock until it exits.
lock_sc_calls() {
  local home lock
  id "$CLI_USER" >/dev/null 2>&1 || return 0  # first install: nothing can call sc yet
  home="$(getent passwd "$CLI_USER" | cut -d: -f6)"
  [[ -n "$home" && -d "$home" ]] || return 0  # the wrapper refuses to start sc without it
  lock="$home/.config/scalable-cli/.quantfolio-sc.lock"
  install -d -o "$CLI_USER" -g "$CLI_USER" -m 0700 "$home/.config" "$home/.config/scalable-cli"
  # Create it as the CLI user if it is missing: the wrapper must be able to open it.
  runuser -u "$CLI_USER" -- sh -c ': >>"$1"' sh "$lock"
  exec 9<"$lock"
  if ! flock -n 9; then
    echo "Waiting for a running sc call (a sync or a login) to finish..."
    flock -w 900 9 || die "an sc call has been running for 15 minutes; the glibc for sc was not changed, run this again later"
  fi
}
unlock_sc_calls() { exec 9<&-; }

# Debian 13's glibc, for sc only, in $RUNTIME: the loader as ld.so, libc and
# libgcc_s next to it. apt checks the packages against Debian's signed archive.
install_runtime() {
  local deb_arch triplet loader keyring=/usr/share/keyrings/debian-archive-keyring.gpg
  case "$arch" in
    x86_64) deb_arch=amd64; triplet=x86_64-linux-gnu; loader=ld-linux-x86-64.so.2 ;;
    aarch64) deb_arch=arm64; triplet=aarch64-linux-gnu; loader=ld-linux-aarch64.so.1 ;;
  esac
  [[ -f "$keyring" ]] || die "$keyring is missing (apt-get install debian-archive-keyring)"
  local apt_dir="$work/apt"
  mkdir -p "$apt_dir/lists/partial" "$apt_dir/archives/partial" "$apt_dir/debs"
  chmod 0755 "$work" "$apt_dir"  # apt downloads as the _apt user
  chown _apt "$apt_dir/lists/partial" "$apt_dir/archives/partial" "$apt_dir/debs" 2>/dev/null || true
  cat >"$apt_dir/sources.list" <<EOF
deb [arch=$deb_arch signed-by=$keyring] https://deb.debian.org/debian trixie main
deb [arch=$deb_arch signed-by=$keyring] https://deb.debian.org/debian trixie-updates main
deb [arch=$deb_arch signed-by=$keyring] https://security.debian.org/debian-security trixie-security main
EOF
  local opts=(-q -o "Dir::Etc::sourcelist=$apt_dir/sources.list" -o Dir::Etc::sourceparts=-
    -o "Dir::State::lists=$apt_dir/lists" -o "Dir::Cache::archives=$apt_dir/archives"
    -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= -o Acquire::Languages=none)
  apt-get "${opts[@]}" update >/dev/null \
    || die "could not read Debian 13's signed package lists; nothing was installed"
  (cd "$apt_dir/debs" && apt-get "${opts[@]}" download "libc6:$deb_arch" "libgcc-s1:$deb_arch" >/dev/null) \
    || die "could not download Debian 13's libc6 and libgcc-s1; nothing was installed"
  mkdir "$apt_dir/x"
  local deb
  for deb in "$apt_dir"/debs/*.deb; do dpkg-deb -x "$deb" "$apt_dir/x"; done
  local lib="$apt_dir/x/usr/lib/$triplet" f
  for f in "$loader" libc.so.6 libgcc_s.so.1; do
    [[ -f "$lib/$f" ]] || die "$f is not in Debian's packages; nothing was installed"
  done
  rm -rf "$RUNTIME.new"
  install -d -o root -g root -m 0755 "$RUNTIME.new"
  install -o root -g root -m 0755 "$lib/$loader" "$RUNTIME.new/ld.so"
  install -o root -g root -m 0644 "$lib/libc.so.6" "$lib/libgcc_s.so.1" "$RUNTIME.new/"
  for deb in "$apt_dir"/debs/*.deb; do
    dpkg-deb --showformat='${Package} ${Version}\n' -W "$deb"
  done >"$RUNTIME.new/SOURCE"
  chmod 0644 "$RUNTIME.new/SOURCE"
  "$RUNTIME.new/ld.so" --library-path "$RUNTIME.new" "$SC" --version \
    || die "sc does not run with Debian 13's glibc either; nothing was changed"
  lock_sc_calls
  rm -rf "$RUNTIME"
  mv "$RUNTIME.new" "$RUNTIME"
  unlock_sc_calls
  echo "sc runs with: $(paste -sd',' "$RUNTIME/SOURCE" | sed 's/,/, /g') (Debian 13), in $RUNTIME"
}

if sc_out="$("$SC" --version 2>&1)"; then
  echo "$sc_out"
  if [[ -d "$RUNTIME" ]]; then
    lock_sc_calls
    rm -rf "$RUNTIME"
    unlock_sc_calls
    echo "This system's glibc runs sc now; removed $RUNTIME."
  fi
elif grep -q "version .GLIBC_[0-9.]*. not found" <<<"$sc_out"; then
  say "sc needs a newer glibc than this system's $(ldd --version | head -n 1 | awk '{print $NF}'); fetching Debian 13's for sc only"
  install_runtime
else
  die "the installed sc does not run: $sc_out"
fi

say "Creating the user that owns the Scalable session ($CLI_USER)"
if ! id "$CLI_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$CLI_HOME" --shell /usr/sbin/nologin "$CLI_USER"
fi
chmod 0700 "$CLI_HOME"
install -d -o "$CLI_USER" -g "$CLI_USER" -m 0700 "$CLI_HOME/.config" "$CLI_HOME/.config/scalable-cli"
# Never overwrite a config someone edited by hand, but always keep the trade guard.
config="$CLI_HOME/.config/scalable-cli/config.toml"
if [[ ! -f "$config" ]]; then
  install -o "$CLI_USER" -g "$CLI_USER" -m 0600 "$HERE/config.toml" "$config"
elif ! grep -Eq '^[[:space:]]*allowed_isins[[:space:]]*=[[:space:]]*\[[[:space:]]*\]' "$config"; then
  cp -p "$config" "$config.bak.$(date +%Y%m%d%H%M%S)"
  install -o "$CLI_USER" -g "$CLI_USER" -m 0600 "$HERE/config.toml" "$config"
  echo "Replaced $config: it did not set allowed_isins = [] (backup kept next to it)."
fi

say "Installing the read-only wrapper and the sudoers rule"
install -d -o root -g root -m 0755 "$(dirname "$WRAPPER")"
install -o root -g root -m 0755 "$HERE/quantfolio-sc-ro" "$WRAPPER"
tmp_sudoers="$(mktemp)"
sed "s/^quantfolio ALL=/${APP_USER} ALL=/" "$HERE/sudoers.quantfolio-sc" > "$tmp_sudoers"
visudo -cqf "$tmp_sudoers" || die "the sudoers rule does not parse; nothing was changed in /etc/sudoers.d"
install -o root -g root -m 0440 "$tmp_sudoers" "$SUDOERS"
rm -f "$tmp_sudoers"

say "Checking the path Quantfolio uses (as $APP_USER, through sudo and the wrapper)"
out="$(runuser -u "$APP_USER" -- sudo -n -H -u "$CLI_USER" -- "$WRAPPER" capabilities --json 2>&1)" \
  || die "the service user cannot run the wrapper: $out"
# The same test as the app's trade-guard check (scalable/service.py guard_attested).
python3 - "$out" <<'PY' || die "sc does not report allowed_isins = [] (every order refused); check $config"
import json, sys
env = json.loads(sys.argv[1].strip().splitlines()[-1])
c = (env.get("data") or {}).get("local_trade_controls") or {}
ok = env.get("ok") and c.get("enabled") and c.get("isin_controls_active") \
    and c.get("allowed_isins_configured") and c.get("allowed_isins") == []
sys.exit(0 if ok else 1)
PY
echo "OK: sudo, the wrapper and sc work, and every order is refused locally."

# The same check inside the API unit's sandbox, if systemd can run one here.
# Besides NoNewPrivileges itself, every setting that installs a seccomp filter
# turns it on for a non-root service, and sudo then cannot switch user.
if command -v systemd-run >/dev/null 2>&1 && systemctl cat quantfolio-api >/dev/null 2>&1; then
  props=() blockers=()
  while IFS='=' read -r key value; do
    case "$key" in
      NoNewPrivileges|RestrictSUIDSGID|ProtectKernelTunables|ProtectKernelModules|ProtectKernelLogs|\
      ProtectClock|ProtectHostname|PrivateDevices|LockPersonality|MemoryDenyWriteExecute|RestrictRealtime|\
      RestrictNamespaces|RestrictAddressFamilies|SystemCallFilter|SystemCallArchitectures|SystemCallLog)
        props+=(-p "$key=$value")
        case "${value,,}" in ""|no|false|off|0) ;; *) blockers+=("$key") ;; esac ;;
      ProtectSystem|ProtectControlGroups|ProtectHome|PrivateTmp)
        props+=(-p "$key=$value") ;;
    esac
  done < <(systemctl cat quantfolio-api 2>/dev/null | grep -E '^[A-Za-z]+=' )
  if sandboxed="$(systemd-run --quiet --wait --pipe --collect --uid="$APP_USER" "${props[@]}" \
        sudo -n -H -u "$CLI_USER" -- "$WRAPPER" capabilities --json 2>&1)" && grep -q '"ok":true' <<<"$sandboxed"; then
    echo "OK: sudo also works inside quantfolio-api's systemd sandbox (${props[*]:-no extra directives})."
  else
    echo "WARNING: inside quantfolio-api's sandbox sudo failed: $sandboxed"
    if ((${#blockers[@]})); then
      echo "         The unit sets ${blockers[*]}, which stops sudo for a non-root service."
    fi
    echo "         Install the current units (run quantfolio-update-app) and keep these settings"
    echo "         out of any drop-in (docs/scalable.md, systemd units)."
  fi
fi

cat <<EOF

Done. sc $tag is installed read-only. Next, in Quantfolio:

  1. In the Scalable web app: Profile > Security > Agentic Investing > enable Scalable CLI.
  2. Control Center > Banks & brokers > Scalable Capital:
       Pin installed sc  ->  Log in (approve the code in the Scalable app)  ->  Sync on  ->  Test.
  3. Portfolio > Accounts > Sync Scalable for the first sync.
EOF
