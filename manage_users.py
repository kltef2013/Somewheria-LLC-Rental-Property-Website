#!/usr/bin/env python3
"""
Script to manage authorized users for the application
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def show_current_users():
    """Show currently authorized users"""
    authorized_users = os.getenv('AUTHORIZED_USERS', '').split(',')
    authorized_users = [email.strip() for email in authorized_users if email.strip()]
    
    print("🔐 Current Authorized Users:")
    print("=" * 40)
    
    if not authorized_users:
        print("❌ No authorized users configured")
        print("   Anyone with a Google account can login")
    else:
        for i, email in enumerate(authorized_users, 1):
            print(f"{i}. {email}")
    
    print("=" * 40)

def add_user(email):
    """Add a user to the authorized list"""
    env_file = Path('.env')
    
    if not env_file.exists():
        print("❌ .env file not found!")
        return
    
    # Read current .env file
    with open(env_file, 'r') as f:
        lines = f.readlines()
    
    # Find current AUTHORIZED_USERS line
    current_users = []
    user_line_index = -1
    
    for i, line in enumerate(lines):
        if line.startswith('AUTHORIZED_USERS='):
            user_line_index = i
            current_users = [u.strip() for u in line.split('=', 1)[1].strip().split(',') if u.strip()]
            break
    
    # Add new user if not already present
    email = email.strip().lower()
    if email in current_users:
        print(f"⚠️  User {email} is already authorized")
        return
    
    current_users.append(email)
    
    # Update or add AUTHORIZED_USERS line
    new_line = f"AUTHORIZED_USERS={','.join(current_users)}\n"
    
    if user_line_index >= 0:
        lines[user_line_index] = new_line
    else:
        lines.append(new_line)
    
    # Write back to .env file
    with open(env_file, 'w') as f:
        f.writelines(lines)
    
    print(f"✅ Added {email} to authorized users")
    print(f"Total authorized users: {len(current_users)}")

def remove_user(email):
    """Remove a user from the authorized list"""
    env_file = Path('.env')
    
    if not env_file.exists():
        print("❌ .env file not found!")
        return
    
    # Read current .env file
    with open(env_file, 'r') as f:
        lines = f.readlines()
    
    # Find current AUTHORIZED_USERS line
    current_users = []
    user_line_index = -1
    
    for i, line in enumerate(lines):
        if line.startswith('AUTHORIZED_USERS='):
            user_line_index = i
            current_users = [u.strip() for u in line.split('=', 1)[1].strip().split(',') if u.strip()]
            break
    
    # Remove user
    email = email.strip().lower()
    if email not in current_users:
        print(f"⚠️  User {email} is not in the authorized list")
        return
    
    current_users.remove(email)
    
    # Update AUTHORIZED_USERS line
    if current_users:
        new_line = f"AUTHORIZED_USERS={','.join(current_users)}\n"
    else:
        new_line = "AUTHORIZED_USERS=\n"
    
    if user_line_index >= 0:
        lines[user_line_index] = new_line
    
    # Write back to .env file
    with open(env_file, 'w') as f:
        f.writelines(lines)
    
    print(f"✅ Removed {email} from authorized users")
    print(f"Total authorized users: {len(current_users)}")

def main():
    import sys
    
    if len(sys.argv) < 2:
        print("🔐 Authorized Users Management")
        print("=" * 40)
        print("Usage:")
        print("  python manage_users.py show                    - Show current users")
        print("  python manage_users.py add <email>            - Add a user")
        print("  python manage_users.py remove <email>         - Remove a user")
        print("  python manage_users.py clear                  - Clear all users (allow anyone)")
        print()
        show_current_users()
        return
    
    command = sys.argv[1].lower()
    
    if command == 'show':
        show_current_users()
    
    elif command == 'add':
        if len(sys.argv) < 3:
            print("❌ Please provide an email address")
            return
        add_user(sys.argv[2])
    
    elif command == 'remove':
        if len(sys.argv) < 3:
            print("❌ Please provide an email address")
            return
        remove_user(sys.argv[2])
    
    elif command == 'clear':
        env_file = Path('.env')
        if not env_file.exists():
            print("❌ .env file not found!")
            return
        
        with open(env_file, 'r') as f:
            lines = f.readlines()
        
        for i, line in enumerate(lines):
            if line.startswith('AUTHORIZED_USERS='):
                lines[i] = "AUTHORIZED_USERS=\n"
                break
        
        with open(env_file, 'w') as f:
            f.writelines(lines)
        
        print("✅ Cleared all authorized users")
        print("   Anyone with a Google account can now login")
    
    else:
        print(f"❌ Unknown command: {command}")

if __name__ == "__main__":
    main() 