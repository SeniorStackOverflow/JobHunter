#!/system/bin/sh
# Use the operator-installed native Docker; own only JobHunter's HTTP relay.
set -eu
ROOT=/data/local/jobhunter-dev
BUSYBOX=/data/adb/magisk/busybox
CLI=/data/local/tmp/codex-a51-docker-bin/docker
SOCKET=/data/local/tmp/codex-a51-docker/docker.sock
[ "$(id -u)" = 0 ]
[ "$(getprop ro.product.model)" = SM-A515F ]
[ "$(uname -r)" = 4.14.113-22755563-docker ]
if ! "$CLI" --host "unix://$SOCKET" info >/dev/null 2>&1; then
  # Do not restart or replace an existing daemon, including one still starting.
  if pidof dockerd >/dev/null; then
    echo 'Native Docker exists but is not ready; inspect it before retrying' >&2
    exit 1
  fi
  nohup "$ROOT/bin/native-launcher" /data/local/tmp/codex-a51-docker-bin/dockerd \
    --host "unix://$SOCKET" --group 0 \
    --data-root /data/local/tmp/codex-a51-docker/data \
    --exec-root /data/local/tmp/codex-a51-docker/exec \
    --pidfile /data/local/tmp/codex-a51-docker/dockerd.pid \
    --dns 1.1.1.1 --dns 8.8.8.8 --storage-driver overlay2 \
    --exec-opt native.cgroupdriver=cgroupfs \
    >"$ROOT/dockerd.log" 2>&1 </dev/null &
  for _attempt in $(seq 1 30); do
    "$CLI" --host "unix://$SOCKET" info >/dev/null 2>&1 && break
    sleep 1
  done
  "$CLI" --host "unix://$SOCKET" info >/dev/null
  /system/bin/sh /data/local/tmp/docker-network.sh start \
    "$(cat /data/local/tmp/codex-a51-docker/dockerd.pid)"
fi
[ -f /data/local/tmp/codex-a51-docker-network/ready ]
# A native CLI-only chroot gives Go tools normal DNS/CA paths. No guest kernel/VM.
HOST="$ROOT/cli-rootfs"
bind() {
  mkdir -p "$2"
  "$BUSYBOX" mountpoint -q "$2" || mount -o bind "$1" "$2"
}
bind /proc "$HOST/proc"
bind /dev "$HOST/dev"
bind /data/local/tmp/codex-a51-docker-bin "$HOST/data/local/tmp/codex-a51-docker-bin"
bind /data/local/tmp/codex-a51-docker "$HOST/data/local/tmp/codex-a51-docker"
bind "$ROOT/docker-config" "$HOST$ROOT/docker-config"
bind "$ROOT/releases" "$HOST/opt/jobhunter-dev/releases"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$HOST/etc/resolv.conf"
mkdir -p "$ROOT/bin"
if [ ! -x "$ROOT/bin/docker-proxy" ]; then
  cp /data/local/tmp/codex-a51-docker-bin/docker-proxy "$ROOT/bin/docker-proxy"
fi
chmod 700 "$ROOT/bin/docker-proxy"
relay() {
  NAME=$1 HOST_IP=$2 HOST_PORT=$3 TARGET_IP=$4 TARGET_PORT=$5 NETNS=${6:-}
  BOOT=$(cat /proc/sys/kernel/random/boot_id)
  EXPECTED="$ROOT/bin/docker-proxy -proto tcp -host-ip $HOST_IP -host-port $HOST_PORT -container-ip $TARGET_IP -container-port $TARGET_PORT "
  if [ -f "$ROOT/$NAME.pid" ]; then
    PID=$(cat "$ROOT/$NAME.pid")
    case "$PID" in ''|*[!0-9]*) echo 'Invalid owned relay PID' >&2; exit 1;; esac
    PREVIOUS_BOOT=$(cat "$ROOT/$NAME.boot" 2>/dev/null || true)
    # A PID saved before reboot may now belong to an unrelated Android process.
    if [ -z "$PREVIOUS_BOOT" ] || [ "$PREVIOUS_BOOT" = "$BOOT" ]; then
      if [ -r "/proc/$PID/cmdline" ]; then
        COMMAND=$(tr '\000' ' ' < "/proc/$PID/cmdline")
        case "$COMMAND" in
          "$ROOT/bin/docker-proxy -proto tcp -host-ip $HOST_IP -host-port $HOST_PORT -container-ip "*" -container-port $TARGET_PORT "*) ;;
          *) echo 'Relay PID belongs to another process; inspect before replacing' >&2; exit 1;;
        esac
        if [ "$COMMAND" = "$EXPECTED" ]; then
          printf '%s\n' "$BOOT" > "$ROOT/$NAME.boot"
          return
        fi
        # Only replace the verified owned relay when the DEV container IP changes.
        kill "$PID"
        for _attempt in 1 2 3 4 5; do
          [ ! -e "/proc/$PID" ] && break
          sleep 1
        done
        [ ! -e "/proc/$PID" ] || { echo 'Owned relay did not stop' >&2; exit 1; }
      fi
    fi
  fi
  rm -f "$ROOT/$NAME.ready"
  if [ -n "$NETNS" ]; then
    nohup nsenter -t "$NETNS" -n -- "$ROOT/bin/docker-proxy" \
      -proto tcp -host-ip "$HOST_IP" -host-port "$HOST_PORT" \
      -container-ip "$TARGET_IP" -container-port "$TARGET_PORT" \
      3>"$ROOT/$NAME.ready" >"$ROOT/$NAME.log" 2>&1 </dev/null &
  else
    nohup "$ROOT/bin/docker-proxy" -proto tcp -host-ip "$HOST_IP" -host-port "$HOST_PORT" \
      -container-ip "$TARGET_IP" -container-port "$TARGET_PORT" \
      3>"$ROOT/$NAME.ready" >"$ROOT/$NAME.log" 2>&1 </dev/null &
  fi
  PID=$!
  printf '%s\n' "$PID" > "$ROOT/$NAME.pid"
  printf '%s\n' "$BOOT" > "$ROOT/$NAME.boot"
  for _attempt in 1 2 3 4 5; do
    if [ -s "$ROOT/$NAME.ready" ]; then
      [ "$(head -n 1 "$ROOT/$NAME.ready")" = 0 ] && kill -0 "$PID" && return
      cat "$ROOT/$NAME.ready" >&2
      exit 1
    fi
    kill -0 "$PID" || exit 1
    sleep 1
  done
  echo 'DEV HTTP relay did not become ready' >&2
  exit 1
}
# Docker intentionally does not publish ports from an internal bridge. Keep that
# bridge internal and explicitly forward only HTTP to the verified DEV API IP.
API_IP=$("$CLI" --host "unix://$SOCKET" inspect --format \
  '{{(index .NetworkSettings.Networks "jobhunter-dev-a51_dev").IPAddress}}' \
  jobhunter-dev-a51-api-1 2>/dev/null || true)
case "$API_IP" in
  172.*)
    relay api-relay 10.231.43.2 8882 "$API_IP" 8000 \
      "$(cat /data/local/tmp/codex-a51-docker/dockerd.pid)"
    ;;
  '') ;; # First setup precedes creation of the application container.
  *) echo 'Unexpected DEV API bridge address' >&2; exit 1;;
esac
relay relay 0.0.0.0 8881 10.231.43.2 8882
