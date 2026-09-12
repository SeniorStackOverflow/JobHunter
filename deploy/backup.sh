#!/bin/sh
set -eu

: "${BACKUP_DIR:=/backups}"
: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
database_name="$POSTGRES_DB"

case "$BACKUP_DIR" in
    /backups | /backups/*) ;;
    *)
        echo "BACKUP_DIR must be /backups or a child directory" >&2
        exit 64
        ;;
esac

install -d -m 0700 "$BACKUP_DIR"
backup_name="job-agent-${database_name}-current.dump"
temporary_file="${BACKUP_DIR}/.${backup_name}.$$.partial"
temporary_checksum="${temporary_file}.sha256"
final_file="${BACKUP_DIR}/${backup_name}"
final_checksum="${final_file}.sha256"

cleanup() {
    rm -f -- "$temporary_file" "$temporary_checksum"
}
trap cleanup EXIT HUP INT TERM

export PGPASSWORD="$POSTGRES_PASSWORD"
pg_dump \
    --host="$POSTGRES_HOST" \
    --port="$POSTGRES_PORT" \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --format=custom \
    --compress=6 \
    --no-owner \
    --no-acl \
    --file="$temporary_file"

pg_restore --list "$temporary_file" >/dev/null
chmod 0600 "$temporary_file"
checksum="$(sha256sum "$temporary_file" | awk '{print $1}')"
printf '%s  %s\n' "$checksum" "$backup_name" > "$temporary_checksum"
chmod 0600 "$temporary_checksum"
mv -f -- "$temporary_file" "$final_file"
mv -f -- "$temporary_checksum" "$final_checksum"
trap - EXIT HUP INT TERM

for stale_file in "$BACKUP_DIR"/"job-agent-${database_name}-"*.dump "$BACKUP_DIR"/"job-agent-${database_name}-"*.dump.sha256; do
    [ -e "$stale_file" ] || continue
    case "$stale_file" in
        "$final_file"|"$final_checksum") continue ;;
    esac
    rm -f -- "$stale_file"
done

echo "Backup created: $final_file"
