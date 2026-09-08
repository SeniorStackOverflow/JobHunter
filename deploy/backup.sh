#!/bin/sh
set -eu

: "${BACKUP_DIR:=/backups}"

if [ -n "${MIGRATOR_DATABASE_URL:-}" ]; then
    case "$MIGRATOR_DATABASE_URL" in
        postgresql+asyncpg://*)
            database_url="postgresql://${MIGRATOR_DATABASE_URL#postgresql+asyncpg://}"
            ;;
        postgresql://* | postgres://*)
            database_url="$MIGRATOR_DATABASE_URL"
            ;;
        *)
            echo "MIGRATOR_DATABASE_URL must use a PostgreSQL URL scheme" >&2
            exit 64
            ;;
    esac
    database_name=${database_url##*/}
    database_name=${database_name%%\?*}
    database_name=${database_name%%\#*}
    [ -n "$database_name" ] || {
        echo "MIGRATOR_DATABASE_URL must include a database name" >&2
        exit 64
    }
    export PGDATABASE="$database_url"
    unset PGPASSWORD
else
    : "${POSTGRES_HOST:?POSTGRES_HOST is required}"
    : "${POSTGRES_PORT:=5432}"
    : "${POSTGRES_DB:?POSTGRES_DB is required}"
    : "${POSTGRES_USER:?POSTGRES_USER is required}"
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
    database_name="$POSTGRES_DB"
fi

case "$BACKUP_DIR" in
    /backups | /backups/*) ;;
    *)
        echo "BACKUP_DIR must be /backups or a child directory" >&2
        exit 64
        ;;
esac

install -d -m 0700 "$BACKUP_DIR"
backup_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_name="job-agent-${database_name}-${backup_timestamp}.dump"
temporary_file="${BACKUP_DIR}/.${backup_name}.partial"
final_file="${BACKUP_DIR}/${backup_name}"

cleanup() {
    rm -f -- "$temporary_file"
}
trap cleanup EXIT HUP INT TERM

if [ -n "${MIGRATOR_DATABASE_URL:-}" ]; then
    pg_dump \
        --format=custom \
        --compress=6 \
        --no-owner \
        --no-acl \
        --file="$temporary_file"
else
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
fi

pg_restore --list "$temporary_file" >/dev/null
chmod 0600 "$temporary_file"
mv -- "$temporary_file" "$final_file"
trap - EXIT HUP INT TERM

sha256sum "$final_file" > "${final_file}.sha256"
chmod 0600 "${final_file}.sha256"
echo "Backup created: $final_file"
