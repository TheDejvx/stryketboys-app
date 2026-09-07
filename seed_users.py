"""One-off / re-runnable seed script for the initial user accounts.
Run with `python seed_users.py` to (re)write stryk_data.json's `users` list
with freshly hashed passwords, preserving any existing `weeks`.
"""
import json
import os
from werkzeug.security import generate_password_hash

DATA_FILE = os.path.join(os.path.dirname(__file__), 'stryk_data.json')

SEED_USERS = [
    ('victor', 'Victor Hagen', 'hagen'),
    ('tommy', 'Tommy Selander', 'selander'),
    ('david', 'David Selander', 'selander'),
    ('martin', 'Martin Selander', 'selander'),
    ('gustav', 'Gustav Molander', 'molander'),
    ('olle', 'Olle Ahlén', 'ahlén'),
]

if os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
else:
    data = {}

data['users'] = [
    {
        'username': username,
        'display_name': display,
        'password_hash': generate_password_hash(pw),
        'must_change_password': True,
    }
    for username, display, pw in SEED_USERS
]
data.setdefault('weeks', [])
data.pop('boys', None)

with open(DATA_FILE, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"Seeded {len(data['users'])} users into {DATA_FILE}")
