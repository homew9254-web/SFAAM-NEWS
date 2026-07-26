#!/usr/bin/env python3
"""
SFAAM NEWS - Admin Credential Generator
========================================
Generates a secure admin password hash and API key for the SFAAM NEWS platform.

Usage:
    python3 scripts/gen_admin_creds.py                  # auto-generate everything
    python3 scripts/gen_admin_creds.py --password "MyPass"  # hash a specific password

Output:
    ADMIN_PASSWORD_HASH=...
    ADMIN_KEY=...

Copy these values into your Railway Variables tab or local .env file.
"""
import argparse
import hashlib
import secrets
import string


def generate_password(length: int = 24) -> str:
    """Generate a memorable-yet-strong password."""
    words = [
        "Ocean", "Glacier", "Dragon", "Aurora", "Phoenix", "Cipher",
        "Comet", "Ember", "Falcon", "Horizon", "Meteor", "Nebula",
        "Orbit", "Pulsar", "Quasar", "Raptor", "Specter", "Thunder",
        "Vortex", "Zenith", "Bolt", "Crimson", "Delta", "Echo"
    ]
    import random
    chosen = random.sample(words, 4)
    num = random.randint(100, 999)
    suffix = random.choice(["?", "!", "#", "*"])
    return f"{chosen[0]}-{chosen[1]}-{chosen[2]}-{chosen[3]}-{num}{suffix}"


def hash_password(password: str) -> str:
    """SHA-256 hash of password (must match main.py's _verify_admin_password)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Generate a secure 43-char URL-safe API key."""
    return secrets.token_urlsafe(32)


def main():
    parser = argparse.ArgumentParser(description="Generate SFAAM admin credentials")
    parser.add_argument("--password", help="Hash a specific password (otherwise auto-generate)")
    parser.add_argument("--length", type=int, default=24, help="Generated password length (ignored if --password)")
    args = parser.parse_args()

    password = args.password if args.password else generate_password()
    pw_hash = hash_password(password)
    api_key = generate_api_key()

    print("=" * 60)
    print("  SFAAM NEWS - ADMIN CREDENTIALS")
    print("=" * 60)
    print()
    print("Add these to your Railway Variables tab (or .env file):")
    print()
    print(f"  ADMIN_PASSWORD_HASH={pw_hash}")
    print(f"  ADMIN_KEY={api_key}")
    print()
    print("-" * 60)
    print("  PLAINTEXT PASSWORD (keep this safe, do NOT commit):")
    print("-" * 60)
    print(f"  {password}")
    print("-" * 60)
    print()
    print("Login at: https://your-domain/admin.html")
    print(f"Password: {password}")
    print()
    print("To rotate credentials later, re-run this script and update env vars.")


if __name__ == "__main__":
    main()
