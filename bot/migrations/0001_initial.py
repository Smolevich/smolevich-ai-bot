from yoyo import step

__depends__ = {}

steps = [
    step(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, is_allowed INTEGER DEFAULT 0, username TEXT, first_name TEXT)",
        "DROP TABLE users"
    ),
    step(
        "CREATE TABLE sessions (user_id INTEGER PRIMARY KEY, model TEXT, history_json TEXT DEFAULT '[]')",
        "DROP TABLE sessions"
    )
]
