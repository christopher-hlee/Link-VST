"""Generate a MONITOR_PASSWORD_HASH. Run on the server:

    python -m monitor.hashpw

Prints the line to paste into monitor/.env. The password is never echoed and
never leaves the machine.
"""
import getpass
import secrets
import sys

from .security import hash_password


def main() -> int:
    password = getpass.getpass("Dashboard password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm: "):
        print("Passwords did not match.", file=sys.stderr)
        return 1

    print()
    print(f"MONITOR_PASSWORD_HASH={hash_password(password)}")
    print(f"SESSION_SECRET={secrets.token_urlsafe(48)}")
    print()
    print("Paste both lines into monitor/.env, then restart the service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
