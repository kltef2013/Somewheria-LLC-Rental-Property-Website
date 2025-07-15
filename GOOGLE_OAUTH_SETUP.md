# Google OAuth Setup Guide

This guide will help you set up Google Sign-In for your Flask application with user access control.

## Prerequisites

1. A Google account
2. Python 3.7+ installed
3. The required Python packages (install with `pip install -r requirements.txt`)

## Step 1: Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google+ API:
   - Go to "APIs & Services" > "Library"
   - Search for "Google+ API" and enable it
   - Also enable "Google Identity and Access Management (IAM) API"

## Step 2: Create OAuth 2.0 Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth 2.0 Client IDs"
3. Choose "Web application" as the application type
4. Add the following authorized redirect URIs:
   - `http://localhost:5000/google/callback` (for local development)
   - `https://yourdomain.com/google/callback` (for production)
5. Click "Create"
6. Note down your Client ID and Client Secret

## Step 3: Configure Environment Variables

Create a `.env` file in your project root with the following variables:

```env
# Flask Secret Key (generate a random one)
SECRET_KEY=your-secret-key-here

# Email Configuration
EMAIL_APP_PASSWORD=your-gmail-app-password

# Google OAuth Configuration
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=http://localhost:5000/google/callback

# Authorized Users (comma-separated email addresses)
# Leave empty to allow anyone with a Google account
AUTHORIZED_USERS=user1@example.com,user2@example.com
```

## Step 4: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 5: Configure Authorized Users (Optional)

By default, anyone with a Google account can login. To restrict access to specific users:

```bash
# Show current authorized users
python manage_users.py show

# Add an authorized user
python manage_users.py add user@example.com

# Remove an authorized user
python manage_users.py remove user@example.com

# Clear all restrictions (allow anyone)
python manage_users.py clear
```

## Step 6: Run the Application

```bash
python website_app.py
```

## Features Added

- **Google Sign-In Button**: Users can sign in with their Google account
- **Session Management**: User sessions are maintained across requests
- **User Profile Display**: Shows user name, email, and profile picture
- **Protected Routes**: The manage listings page now requires authentication
- **Logout Functionality**: Users can log out and clear their session
- **User Access Control**: Only authorized email addresses can access the application
- **Access Management**: Easy tools to add/remove authorized users

## Security Notes

- The application uses Flask sessions for user management
- Google OAuth tokens are verified server-side
- HTTPS is required for production (disable `OAUTHLIB_INSECURE_TRANSPORT` in production)
- Store sensitive credentials in environment variables, never in code

## Troubleshooting

1. **"Google OAuth not configured" error**: Make sure your environment variables are set correctly
2. **Redirect URI mismatch**: Ensure the redirect URI in Google Console matches your application
3. **Session not working**: Check that `SECRET_KEY` is set in your environment
4. **HTTPS errors in development**: The app is configured to allow HTTP for local development

## Production Deployment

For production deployment:

1. Remove or set `OAUTHLIB_INSECURE_TRANSPORT=0`
2. Use HTTPS for all URLs
3. Update the redirect URI to your production domain
4. Use a strong, random `SECRET_KEY`
5. Consider using a proper session store (Redis, database) for scalability 