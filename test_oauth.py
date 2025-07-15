#!/usr/bin/env python3
"""
Test script to verify Google OAuth configuration
"""

import os
from dotenv import load_dotenv

load_dotenv()

def test_oauth_config():
    """Test if Google OAuth environment variables are configured"""
    print("Testing Google OAuth Configuration...")
    print("=" * 40)
    
    # Check required environment variables
    required_vars = [
        'GOOGLE_CLIENT_ID',
        'GOOGLE_CLIENT_SECRET',
        'SECRET_KEY'
    ]
    
    # Check authorized users configuration
    authorized_users = os.getenv('AUTHORIZED_USERS', '').split(',')
    authorized_users = [email.strip() for email in authorized_users if email.strip()]
    
    missing_vars = []
    for var in required_vars:
        value = os.getenv(var)
        if value:
            print(f"✓ {var}: {'*' * min(len(value), 10)}...")
        else:
            print(f"✗ {var}: NOT SET")
            missing_vars.append(var)
    
    print("\n" + "=" * 40)
    
    # Display authorized users info
    print("🔐 Authorized Users Configuration:")
    if authorized_users:
        print(f"✅ {len(authorized_users)} authorized user(s):")
        for email in authorized_users:
            print(f"   • {email}")
    else:
        print("⚠️  No authorized users configured")
        print("   Anyone with a Google account can login")
    
    print("\n" + "=" * 40)
    
    if missing_vars:
        print("❌ Configuration incomplete!")
        print(f"Missing variables: {', '.join(missing_vars)}")
        print("\nPlease set the missing environment variables in your .env file.")
        print("See GOOGLE_OAUTH_SETUP.md for detailed instructions.")
        return False
    else:
        print("✅ Configuration looks good!")
        print("\nNext steps:")
        print("1. Make sure you've created OAuth 2.0 credentials in Google Cloud Console")
        print("2. Set the redirect URI to: http://localhost:5000/google/callback")
        print("3. Run: python website_app.py")
        print("4. Visit: http://localhost:5000/login")
        if not authorized_users:
            print("5. Add authorized users with: python manage_users.py add <email>")
        return True

if __name__ == "__main__":
    test_oauth_config() 