#!/bin/sh
set -eu

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_FILE:?BACKUP_FILE is required}"
: "${RESTORE_CONFIRM:?RESTORE_CONFIRM is required}"

expected_confirmation="restore:${POSTGRES_DB}"
if [ "$RESTORE_CONFIRM" != "$expected_confirmation" ]; then
    echo "Refusing restore: set RESTORE_CONFIRM=$expected_confirmation" >&2
    exit 64
fi

case "$BACKUP_FILE" in
    "$BACKUP_DIR"/*.dump) ;;
    *)
        echo "BACKUP_FILE must be a .dump file directly inside BACKUP_DIR" >&2
        exit 64
        ;;
esac

case "$POSTGRES_DB" in
    postgres | template0 | template1)
        echo "Refusing to restore into a PostgreSQL maintenance database" >&2
        exit 64
        ;;
esac

if [ ! -f "$BACKUP_FILE" ] || [ -L "$BACKUP_FILE" ]; then
    echo "Backup file is missing, not regular, or is a symbolic link" >&2
    exit 66
fi

if [ -f "${BACKUP_FILE}.sha256" ]; then
    (cd "$BACKUP_DIR" && sha256sum -c "$(basename "${BACKUP_FILE}.sha256")")
fi
pg_restore --list "$BACKUP_FILE" >/dev/null

export PGPASSWORD="$POSTGRES_PASSWORD"
pg_restore \
    --host="$POSTGRES_HOST" \
    --port="$POSTGRES_PORT" \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --clean \
    --if-exists \
    --exit-on-error \
    --no-owner \
    --no-acl \
    "$BACKUP_FILE"

echo "Restore completed for database: $POSTGRES_DB"
