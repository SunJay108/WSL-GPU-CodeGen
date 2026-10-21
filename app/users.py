"""
Account maintenance from the command line, e.g. when the admin password is lost:

    docker compose exec codegen python users.py list
    docker compose exec codegen python users.py reset-password admin
    docker compose exec codegen python users.py add alice [--admin]
"""

import argparse
import getpass
import sys

import auth
import db


def ask_password() -> str:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("The passwords do not match.")
    error = auth.password_error(password)
    if error:
        sys.exit(error)
    return password


def main():
    parser = argparse.ArgumentParser(description="CodeGen accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list accounts")
    reset = sub.add_parser("reset-password", help="set a new password and sign the account out everywhere")
    reset.add_argument("username")
    add = sub.add_parser("add", help="create an account")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="make it an administrator")
    args = parser.parse_args()

    db.init_db()

    if args.command == "list":
        for u in db.list_users():
            print(f"{u['username']:<32} {u['role']:<6} {'active' if u['is_active'] else 'disabled'}")
    elif args.command == "reset-password":
        user = db.get_user_by_name(args.username) or sys.exit(f"No user '{args.username}'.")
        db.update_user(user["id"], password_hash=auth.hash_password(ask_password()), is_active=True)
        db.delete_user_sessions(user["id"])
        print(f"Password of '{user['username']}' changed; the account is enabled.")
    elif args.command == "add":
        error = auth.username_error(args.username)
        if error or db.get_user_by_name(args.username):
            sys.exit(error or f"User '{args.username}' already exists.")
        role = "admin" if args.admin else "user"
        db.create_user(args.username, auth.hash_password(ask_password()), role)
        print(f"Created {role} '{args.username}'.")


if __name__ == "__main__":
    main()
