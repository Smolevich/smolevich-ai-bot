#!/usr/bin/env python3
"""Apply database migrations using yoyo-migrations."""
import os
import sys
from yoyo import read_migrations, get_backend

DB_FILE = "/var/lib/telegram-llm-bot.db"
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

def apply_migrations():
    print(f"Applying migrations from {MIGRATIONS_DIR} to {DB_FILE}...")
    
    # Ensure directory exists
    db_dir = os.path.dirname(DB_FILE)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    backend = get_backend(f"sqlite:///{DB_FILE}?timeout=60")
    migrations = read_migrations(MIGRATIONS_DIR)
    
    if not migrations:
        print("No migrations found.")
        return

    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))
    
    # Set permissions
    if os.path.exists(DB_FILE):
        os.chmod(DB_FILE, 0o666)
    
    print("Migrations applied successfully.")

if __name__ == "__main__":
    try:
        apply_migrations()
    except Exception as e:
        print(f"Error applying migrations: {e}")
        sys.exit(1)
