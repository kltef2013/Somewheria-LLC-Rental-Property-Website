import os
import time
import base64
import secrets
import logging
import requests
import smtplib
import json
from io import BytesIO
from PIL import Image, ImageOps
from email.message import EmailMessage
from flask import Flask, jsonify, render_template_string, request, g, url_for, redirect, session
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:5000/google/callback')

# Authorized users whitelist (comma-separated email addresses)
AUTHORIZED_USERS = os.getenv('AUTHORIZED_USERS', '').split(',')
AUTHORIZED_USERS = [email.strip().lower() for email in AUTHORIZED_USERS if email.strip()]

# Disable HTTPS requirement for local development
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# User session management
def is_logged_in():
    return 'user' in session

def get_current_user():
    return session.get('user')

def is_whitelist_configured():
    """Check if authorized users whitelist is configured"""
    return len(AUTHORIZED_USERS) > 0

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Custom 404 error handler
@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html", title="Page Not Found"), 404

# Edit Listing route
@app.route("/edit-listing/<property_id>")
@login_required
def edit_listing(property_id):
    with cache_lock:
        property_data = next((p for p in properties_cache if p.get("id") == property_id), None)
    if not property_data:
        return "Property not found", 404
    return render_template("edit_listing.html", property_id=property_id, property=property_data, user=get_current_user())

# ...existing code...

@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("manage_listings"))
    
    if request.method == "POST":
        return redirect(url_for("manage_listings"))
    
    return render_template("login.html", title="Login", whitelist_configured=is_whitelist_configured())

@app.route("/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Google OAuth not configured", 500
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI]
            }
        },
        scopes=['openid', 'https://www.googleapis.com/auth/userinfo.profile', 'https://www.googleapis.com/auth/userinfo.email']
    )
    
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    
    session['state'] = state
    return redirect(authorization_url)

@app.route("/google/callback")
def google_callback():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Google OAuth not configured", 500
    
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI]
                }
            },
            scopes=['openid', 'https://www.googleapis.com/auth/userinfo.profile', 'https://www.googleapis.com/auth/userinfo.email']
        )
        
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        
        credentials = flow.credentials
        id_info = id_token.verify_oauth2_token(
            credentials.id_token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        
        user_email = id_info['email'].lower()
        
        # Check if user is authorized
        if AUTHORIZED_USERS and user_email not in AUTHORIZED_USERS:
            error_message = f"Unauthorized access attempt by: {user_email}"
            log_and_notify_error("Unauthorized Login Attempt", error_message)
            return render_template("login.html", title="Login", error="Access denied. Your email is not authorized to use this application.")
        
        # Store user information in session
        session['user'] = {
            'id': id_info['sub'],
            'email': id_info['email'],
            'name': id_info.get('name', ''),
            'picture': id_info.get('picture', ''),
            'given_name': id_info.get('given_name', ''),
            'family_name': id_info.get('family_name', '')
        }
        
        # Log successful login
        success_message = f"Successful login by: {user_email}"
        print(f"[AUTH] {success_message}")
        
        return redirect(url_for("manage_listings"))
    except Exception as e:
        error_message = f"Google OAuth callback error: {str(e)}"
        log_and_notify_error("Google OAuth Error", error_message)
        return render_template("login.html", title="Login", error="Authentication failed. Please try again.")

@app.route("/logout")
def logout():
    session.pop('user', None)
    return redirect(url_for('home'))

@app.route("/auth/status")
def auth_status():
    """Check authentication status - useful for debugging"""
    if is_logged_in():
        user = get_current_user()
        return jsonify({
            'authenticated': True,
            'user': {
                'id': user['id'],
                'email': user['email'],
                'name': user['name'],
                'picture': user.get('picture', '')
            }
        })
    else:
        return jsonify({
            'authenticated': False,
            'user': None
        })

# Route for Manage Listings page
@app.route("/manage-listings")
@login_required
def manage_listings():
    with cache_lock:
        properties = list(properties_cache)
    return render_template("manage_listings.html", title="Manage Listings", properties=properties, user=get_current_user())
## ...existing code...
@app.route("/report-issue-complete")
def report_issue_complete():
    return render_template(
        "report_issue.html",
        title="Report an Issue",
        confirmation=True
    )
LOG_FILE = "application.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format='%(asctime)s:%(levelname)s:%(message)s'
)
UPLOAD_FOLDER = os.path.join("static", "uploads")
STATIC_DIR = os.path.join("static")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
PROPERTY_APPTS_FILE = os.path.join(STATIC_DIR, "property_appointments.txt")
########### APPOINTMENT PERSISTENCE ###########
def print_check_file(path, purpose):
    abs_path = os.path.abspath(path)
    exists = os.path.exists(abs_path)
    status = "exists" if exists else "does NOT exist"
    print(f"[CHECK] {purpose} - {abs_path} => {status}")
def load_appointments():
    appts = {}
    abs_path = os.path.abspath(PROPERTY_APPTS_FILE)
    print(f"[LOAD] Attempting to load appointments from: {abs_path}")
    if not os.path.exists(PROPERTY_APPTS_FILE):
        print(f"[INFO] Appointments file does not exist yet: {abs_path}")
        return appts
    with open(PROPERTY_APPTS_FILE, "r", encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                pid, dates = line.split(":", 1)
                date_list = set(x for x in dates.split(",") if x)
                appts[pid.strip()] = set(date_list)
            except Exception:
                continue
    return appts
def save_appointments(appts):
    abs_path = os.path.abspath(PROPERTY_APPTS_FILE)
    print(f"[SAVE] Saving appointments to: {abs_path}")
    with open(PROPERTY_APPTS_FILE, "w", encoding='utf-8') as f:
        for pid, date_set in appts.items():
            out = pid + ":" + ",".join(sorted(date_set))
            print(out, file=f)
    print_check_file(PROPERTY_APPTS_FILE, "Appointments saved")

############## EMAIL and LOGGING ##############
def send_email(subject, body):
    smtp_server = "smtp.gmail.com"
    port = 587
    sender_email = 'anthony.j.ekberg@gmail.com'
    app_password = os.getenv('EMAIL_APP_PASSWORD')
    if not app_password:
        raise ValueError("No app password set in the environment")
    msg = EmailMessage()
    msg.set_content(body)
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = sender_email
    import socket
    try:
        hostname = socket.gethostname()
        ip_addr = socket.gethostbyname(hostname)
        server_url = f"http://{ip_addr}:5000"
    except Exception:
        server_url = "http://localhost:5000"
    body += f"\nView application logs here: {server_url}/logs"
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_server, port) as server:
            server.starttls()
            server.login(sender_email, app_password)
            server.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")
def log_and_notify_error(subject, error_message):
    logging.error(error_message)
    send_email(subject, error_message)
def notify_image_edit(image_urls):
    subject = "Image Edited Notification"
    body = "The following image(s) have been edited:\n" + "\n".join(image_urls)
    send_email(subject, body)
@app.before_request
def before_request():
    g.start_time = time.time()
@app.after_request
def after_request(response):
    try:
        if hasattr(g, "start_time"):
            elapsed_time = time.time() - g.start_time
            if elapsed_time < 0.1:
                elapsed_ms = elapsed_time * 1000
                print(f"Time taken for {request.path}: {elapsed_ms:.2f}ms")
            else:
                print(f"Time taken for {request.path}: {elapsed_time:.2f}s")
    except Exception as e:
        print(f"[after_request error] {e}")
    return response
def letterbox_to_16_9(img: Image.Image) -> Image.Image:
    target_ratio = 16 / 9
    width, height = img.size
    if height == 0: return img
    original_ratio = width / height
    if abs(original_ratio - target_ratio) < 1e-5: return img
    if original_ratio > target_ratio:
        new_width = width
        new_height = int(width / target_ratio)
    else:
        new_height = height
        new_width = int(height * target_ratio)
    new_img = Image.new("RGB", (new_width, new_height), color=(0, 0, 0))
    offset_x = (new_width - width) // 2
    offset_y = (new_height - height) // 2
    new_img.paste(img, (offset_x, offset_y))
    return new_img
def get_base64_image_from_url(url):
    try:
        img_resp = requests.get(url, timeout=10)
        img_resp.raise_for_status()
        with Image.open(BytesIO(img_resp.content)) as img:
            img_16_9 = letterbox_to_16_9(ImageOps.exif_transpose(img).convert("RGB"))
            buffered = BytesIO()
            img_16_9.save(buffered, format="JPEG")
            encoded_img = base64.b64encode(buffered.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{encoded_img}"
    except Exception as err:
        print(f"Could not process image {url}: {err}")
        return None
SHELL = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ title or 'Somewheria, LLC.' }}</title>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?display=swap&family=Noto+Sans:wght@400;500;700;900&family=Plus+Jakarta+Sans:wght@400;500;700;800"/>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <script>
    // Auto-detect system dark mode and apply Tailwind dark class ASAP
    (function() {
      try {
        var darkMode = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        if (darkMode) {
          document.documentElement.classList.add('dark');
        } else {
          document.documentElement.classList.remove('dark');
        }
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
          if (e.matches) {
            document.documentElement.classList.add('dark');
          } else {
            document.documentElement.classList.remove('dark');
          }
        });
      } catch(e) {}
    })();
  </script>
  <style>
    @import url("https://fonts.googleapis.com/css2?family=Poppins:wght@400;700&display=swap");
    .bar1, .bar2, .bar3 {
      width: 35px;
      height: 3px;
      background-color: #111518;
      margin: 6px 0;
      transition: 0.4s;
    }
    #hamburger-icon {
      margin: auto 0;
      display: none;
      cursor: pointer;
    }
    #hamburger-icon.open .bar1 {
      -webkit-transform: rotate(-45deg) translate(-6px, 6px);
      transform: rotate(-45deg) translate(-6px, 6px);
    }
    #hamburger-icon.open .bar2 {
      opacity: 0;
    }
    #hamburger-icon.open .bar3 {
      -webkit-transform: rotate(45deg) translate(-6px, -8px);
      transform: rotate(45deg) translate(-6px, -8px);
    }
    #hamburger-icon .mobile-menu {
      display: none;
      position: absolute;
      top: 50px;
      left: 0;
      height: calc(100vh - 50px);
      width: 100%;
      background: #fff;
      color: #111518;
      transition: background 0.3s, color 0.3s;
    }
    #hamburger-icon.open .mobile-menu {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      background: #fff;
      color: #111518;
    }
    .mobile-menu li {
      margin-bottom: 10px;
    }
    .mobile-menu a {
      transition: color 0.3s, background 0.3s;
    }
    @media (prefers-color-scheme: dark) {
      #hamburger-icon .mobile-menu {
        background: #18181b !important;
        color: #fff !important;
      }
      #hamburger-icon.open .mobile-menu {
        background: #18181b !important;
        color: #fff !important;
      }
      .mobile-menu a {
        color: #fff !important;
      }
    }
    @media only screen and (max-width: 600px) {
      header nav.desktop-nav {
        display: none;
      }
      #hamburger-icon {
        display: block;
      }
    }
    @media only screen and (min-width: 601px) {
      #hamburger-icon {
        display: none !important;
      }
      nav.desktop-nav {
        display: block !important;
      }
    }
    /* Form transitions */
    form input, form textarea, form select {
      transition: background 0.3s, color 0.3s, border-color 0.3s;
    }
    form input:focus, form textarea:focus, form select:focus {
      outline: none;
      border-color: #2563eb;
      background: #f3f4f6;
      color: #111518;
    }
    @media (prefers-color-scheme: dark) {
      form input, form textarea, form select {
        background: #18181b;
        color: #fff;
        border-color: #27272a;
      }
      form input:focus, form textarea:focus, form select:focus {
        background: #27272a;
        color: #fff;
        border-color: #2563eb;
      }
    }
  </style>
</head>
<body style='font-family:"Plus Jakarta Sans","Noto Sans",sans-serif;'>
<div class="relative flex min-h-screen flex-col group/design-root overflow-x-hidden bg-neutral-100 dark:bg-neutral-950">
  <div class="layout-container flex h-full grow flex-col">
    <header class="flex items-center justify-between border-b px-4 sm:px-6 lg:px-10 py-3 w-full bg-neutral-100 dark:bg-neutral-900" style="position:relative;">
      <a id="brand" href="{{ url_for('home') }}" class="flex items-center gap-4 text-[#111518] dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 rounded">
        <div class="size-4">
          <svg viewBox="0 0 48 48" fill="none"><g clip-path="url(#clip0_6_535)">
          <path fill-rule="evenodd" clip-rule="evenodd"
            d="M47.2426 24L24 47.2426L0.757355 24L24 0.757355L47.2426 24ZM12.2426 21H35.7574L24 9.24264L12.2426 21Z"
            fill="currentColor"></path></g>
            <defs><clipPath id="clip0_6_535"><rect width="48" height="48" fill="white"/></clipPath></defs>
          </svg>
        </div>
      </a>
      <nav class="desktop-nav flex-1 hidden sm:block" aria-label="Main navigation">
        <ul class="flex items-center gap-6 justify-center w-full">
          <li><a href="{{ url_for('home') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 dark:hover:text-blue-400 px-3 py-2">Home</a></li>
          <li><a href="{{ url_for('for_rent') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 dark:hover:text-blue-400 px-3 py-2">For Rent</a></li>
          <li><a href="{{ url_for('about') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 dark:hover:text-blue-400 px-3 py-2">About Us</a></li>
          <li><a href="{{ url_for('contact') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 dark:hover:text-blue-400 px-3 py-2">Contact Us</a></li>
        </ul>
      </nav>
      <div class="flex flex-1 justify-end items-center">
        <div id="hamburger-icon" onclick="toggleMobileMenu(this)" aria-label="Open menu" tabindex="0" style="margin-left:auto;">
          <div class="bar1"></div>
          <div class="bar2"></div>
          <div class="bar3"></div>
          <ul class="mobile-menu bg-white text-[#111518] dark:bg-neutral-900 dark:text-white">
            <li><a href="{{ url_for('home') }}" class="text-[#111518] dark:text-white">Home</a></li>
            <li><a href="{{ url_for('for_rent') }}" class="text-[#111518] dark:text-white">For Rent</a></li>
            <li><a href="{{ url_for('about') }}" class="text-[#111518] dark:text-white">About Us</a></li>
            <li><a href="{{ url_for('contact') }}" class="text-[#111518] dark:text-white">Contact Us</a></li>
          </ul>
        </div>
      </div>
    </header>
    <script>
      function toggleMobileMenu(menu) {
        menu.classList.toggle('open');
      }
    </script>
    <main class="flex-1 bg-neutral-100 text-[#111518] dark:bg-neutral-950 dark:text-white">{% block content %}{% endblock %}</main>
    <footer class="py-5 text-center bg-gray-100 text-gray-600 dark:bg-neutral-900 dark:text-gray-300">
      <p class="mb-2">&copy; 2024/25 Somewheria, LLC. All Rights Reserved.</p>
      <div>
        <a href="{{ url_for('report_issue_form') }}" class="text-blue-600 dark:text-blue-400 underline text-sm">Report an issue</a>
      </div>
    </footer>
  </div>
</div>
</body>
</html>
'''
import datetime
property_appointments = load_appointments()
from flask import render_template

@app.route('/')
def home():
    try:
        return render_template("home.html", title="Home")
    except Exception as e:
        print(f"[home error] {e}")
        return "Internal server error", 500
import concurrent.futures
import threading
import time

# --- Caching logic ---
properties_cache = []
cache_lock = threading.Lock()
CACHE_REFRESH_INTERVAL = 600  # seconds (10 minutes)

def preset_fetch_properties():
    global properties_cache
    base = "https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test"
    try:
        resp = requests.get(f"{base}/propertiesforrent", timeout=20)
        resp.raise_for_status()
        uuids = resp.json().get("property_ids", [])
    except Exception:
        uuids = []
    def fetch_property(uuid):
        try:
            d = requests.get(f"{base}/properties/{uuid}/details", timeout=10).json()
            d["photos"] = []
            photo_urls = []
            try:
                photo_urls = requests.get(f"{base}/properties/{uuid}/photos", timeout=10).json()
            except Exception:
                pass
            for photourl in photo_urls:
                b64img = get_base64_image_from_url(photourl)
                if b64img:
                    d["photos"].append(b64img)
            # Fetch thumbnail from /thumbnail endpoint
            try:
                thumbnail_url = requests.get(f"{base}/properties/{uuid}/thumbnail", timeout=10).json()
            except Exception:
                thumbnail_url = None
            d["thumbnail"] = thumbnail_url or (d["photos"][0] if d["photos"] else "")
            d["id"] = uuid
            d.setdefault("included_amenities", d.get("included_utilities", []))
            d.setdefault("bedrooms", "N/A")
            d.setdefault("bathrooms", "N/A")
            d.setdefault("rent", "N/A")
            d.setdefault("sqft", "N/A")
            d.setdefault("deposit", "N/A")
            d.setdefault("address", "N/A")
            # Always set 'description' from API if available
            d["description"] = d.get("description", "")
            d.setdefault("blurb", d["description"])
            d.setdefault("lease_length", d.get("lease_length", "12 months"))
            # Pets allowed logic
            pets_allowed = "Unknown"
            # Direct field
            if "pets_allowed" in d:
                pets_allowed = "Yes" if d["pets_allowed"] else "No"
            # Check amenities
            elif "included_amenities" in d and any("pet" in str(a).lower() for a in d["included_amenities"]):
                pets_allowed = "Yes"
            # Check description
            elif "description" in d and "pet" in d["description"].lower():
                pets_allowed = "Yes"
            d["pets_allowed"] = pets_allowed
            return d
        except Exception as e:
            print(f"Property fetch for {uuid} failed: {e}")
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(fetch_property, uuids))
        with cache_lock:
            properties_cache = [p for p in results if p]
    print(f"[Cache] Refreshed at {time.strftime('%Y-%m-%d %H:%M:%S')}, {len(properties_cache)} properties loaded.")

def periodic_cache_refresh():
    while True:
        try:
            preset_fetch_properties()
        except Exception as e:
            print(f"[Cache] Error during refresh: {e}")
        time.sleep(CACHE_REFRESH_INTERVAL)

# Start periodic cache refresh in background
threading.Thread(target=periodic_cache_refresh, daemon=True).start()

@app.route("/for-rent")
def for_rent():
    # Use cache only, never block for refresh
    with cache_lock:
        properties = list(properties_cache)
    return render_template("for_rent.html", properties=properties, title="For Rent")

@app.route("/for-rent.json")
def for_rent_json():
    # Use cache only, never block for refresh
    with cache_lock:
        properties = list(properties_cache)
    def make_serializable(prop):
        out = dict(prop)
        for k, v in out.items():
            if isinstance(v, set):
                out[k] = list(v)
        return out
    return jsonify([make_serializable(p) for p in properties])
@app.route("/property/<uuid>")
def property_details(uuid):
    try:
        # Use cache for property details
        with cache_lock:
            property_info = next((p for p in properties_cache if p.get("id") == uuid), None)
        if not property_info:
            return "Property not found", 404
        
        # Ensure photos is always a list
        if "photos" not in property_info or not isinstance(property_info["photos"], list):
            property_info["photos"] = []
        
        # Ensure other required fields exist
        property_info.setdefault("name", "Property")
        property_info.setdefault("address", "N/A")
        property_info.setdefault("rent", "N/A")
        property_info.setdefault("deposit", "N/A")
        property_info.setdefault("bedrooms", "N/A")
        property_info.setdefault("bathrooms", "N/A")
        property_info.setdefault("sqft", "N/A")
        property_info.setdefault("lease_length", "12 months")
        property_info.setdefault("included_amenities", [])
        property_info.setdefault("description", "")
        property_info.setdefault("blurb", property_info.get("description", ""))
        property_info.setdefault("pets_allowed", "Unknown")
        property_info.setdefault("thumbnail", property_info["photos"][0] if property_info["photos"] else "")
        
        global property_appointments
        property_appointments = load_appointments()
        booked_dates = sorted(list(property_appointments.get(uuid, set())))
        nowdate = datetime.date.today().strftime("%Y-%m-%d")
        
        return render_template(
            "property_details.html",
            property=property_info,
            nowdate=nowdate,
            booked_dates=booked_dates,
            title=property_info.get("name", "Property"),
        )
    except Exception as e:
        error_message = f"Error loading property {uuid}: {str(e)}"
        log_and_notify_error("Property Details Error", error_message)
        return "Internal server error", 500
@app.route("/property/<uuid>/schedule", methods=["POST"])
def schedule_appointment(uuid):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    date = (data.get("date") or "").strip()
    contact_method = (data.get("contact_method") or "").strip()
    contact_info = (data.get("contact_info") or "").strip()
    tstr = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.date.fromisoformat(date)
    except Exception:
        return jsonify(success=False, error="Invalid date."), 400
    if dt < datetime.date.today():
        return jsonify(success=False, error="Date cannot be in the past."), 400
    # Fetch property live to check existence and get name
    base = "https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test"
    try:
        prop = requests.get(f"{base}/properties/{uuid}/details", timeout=10).json()
        property_name = prop.get("name", "(Unknown Property)")
    except Exception:
        return jsonify(success=False, error="Property not found."), 404
    email_message = (
        f"Appointment requested!\n\n"
        f"Property: {property_name}\n"
        f"Requested by: {name}\n"
        f"For date: {date}\n"
        f"Contact method: {contact_method}\n"
        f"Contact info: {contact_info}\n"
        f"Requested at: {tstr}"
    )
    send_email("Viewing Appointment Request", email_message)
    return jsonify(success=True)
@app.route("/about")
def about():
    return render_template("about.html", title="About")
@app.route("/contact")
def contact():
    return render_template("contact.html", title="Contact")
@app.route("/logs")
def view_logs():
    log_entries = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as log_file:
            for line in log_file:
                line = line.strip()
                if not line:
                    continue
                # Parse log line: '2025-07-12 12:34:56,789:ERROR:message'
                parts = line.split(':', 2)
                if len(parts) == 3:
                    timestamp, level, message = parts
                else:
                    timestamp, level, message = '', '', line
                # Attempt to parse timestamp and replace numbers with words
                def number_to_words(n):
                    words = [
                        'zero','one','two','three','four','five','six','seven','eight','nine','ten',
                        'eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen','twenty',
                        'twenty-one','twenty-two','twenty-three','twenty-four','twenty-five','twenty-six','twenty-seven','twenty-eight','twenty-nine','thirty',
                        'thirty-one','thirty-two','thirty-three','thirty-four','thirty-five','thirty-six','thirty-seven','thirty-eight','thirty-nine','forty',
                        'forty-one','forty-two','forty-three','forty-four','forty-five','forty-six','forty-seven','forty-eight','forty-nine','fifty',
                        'fifty-one','fifty-two','fifty-three','fifty-four','fifty-five','fifty-six','fifty-seven','fifty-eight','fifty-nine'
                    ]
                    try:
                        n = int(n)
                        if 0 <= n < 60:
                            return words[n]
                    except:
                        pass
                    return str(n)
                def timestamp_to_words(ts):
                    import re
                    match = re.match(r'(\d{4})-(\d{2})-(\d{2})[\s\t]+(\d{2})[\t:]+(\d{2})[\t:]+([\d,]+)', ts)
                    if match:
                        year, month, day, hour, minute, second = match.groups()
                        months = ['January','February','March','April','May','June','July','August','September','October','November','December']
                        month_word = months[int(month)-1] if month.isdigit() and 1 <= int(month) <= 12 else month
                        hour_word = number_to_words(hour);
                        minute_word = number_to_words(minute);
                        second_main = second.split(',')[0]
                        second_word = number_to_words(second_main)
                        ms = ''
                        if ',' in second:
                            ms = ',' + second.split(',')[1]
                        return f"{month_word} {number_to_words(day)}, {year} at {hour_word} {minute_word} {second_word}{ms}"
                    return ts
                import re
                ansi_escape = re.compile(r'\x1B\[[0-9;]*[mK]')
                clean_message = ansi_escape.sub('', message)
                # Robust timestamp parsing
                try:
                    ts_words = timestamp_to_words(timestamp)
                except Exception as e:
                    print(f"[LOGS] Failed to parse timestamp: {timestamp} ({e})")
                    ts_words = timestamp or 'Unknown'
                log_entries.append({
                    'timestamp': ts_words,
                    'level': level,
                    'message': clean_message
                })
    return render_template(
        "logs.html",
        log_entries=log_entries,
        title="Logger"
    )

# GET route to render the report issue form
@app.route("/report-issue", methods=["GET"])
def report_issue_form():
    return render_template("report_issue.html", title="Report an Issue", confirmation=False)

# POST route to handle form submission
@app.route("/report-issue", methods=["POST"])
def report_issue():
    user_name = request.form.get("name")
    issue_description = request.form.get("description")
    if not user_name or not issue_description:
        return "Name and description are required fields.", 400
    email_body = f"Issue reported by {user_name}:\n\n{issue_description}"
    send_email("User Reported Issue", email_body)
    return render_template(
        "report_issue.html",
        title="Report an Issue",
        confirmation=True,
        name=user_name,
        desc=issue_description
    )
@app.route("/save-edit/<id>", methods=["POST"])
@login_required
def save_edit(id):
    try:
        # Accept form data
        form = request.form
        with cache_lock:
            prop = next((p for p in properties_cache if p.get("id") == id), None)
            if not prop:
                return "Property not found", 404
            
            # Update basic fields
            prop["name"] = form.get("name", prop.get("name"))
            prop["address"] = form.get("address", prop.get("address"))
            prop["rent"] = form.get("rent", prop.get("rent"))
            prop["deposit"] = form.get("deposit", prop.get("deposit"))
            prop["bedrooms"] = form.get("bedrooms", prop.get("bedrooms"))
            prop["bathrooms"] = form.get("bathrooms", prop.get("bathrooms"))
            prop["lease_length"] = form.get("lease_length", prop.get("lease_length"))
            prop["pets_allowed"] = form.get("pets_allowed", prop.get("pets_allowed"))
            prop["blurb"] = form.get("blurb", prop.get("blurb"))
            prop["description"] = form.get("description", prop.get("description"))
            
            # Handle amenities
            amenities = form.getlist('amenities')  # Get all checked amenities
            custom_amenities = form.get("custom_amenities", "").strip()
            
            # Combine amenities
            all_amenities = amenities.copy()
            if custom_amenities:
                custom_list = [a.strip() for a in custom_amenities.split(',') if a.strip()]
                all_amenities.extend(custom_list)
            
            prop["included_amenities"] = all_amenities
            prop["custom_amenities"] = custom_amenities
            
            # Handle photo deletion
            photos_to_delete = form.get("photos_to_delete")
            if photos_to_delete:
                try:
                    photos_to_delete = json.loads(photos_to_delete)
                    if isinstance(photos_to_delete, list):
                        # Remove selected photos from the property
                        current_photos = prop.get("photos", [])
                        prop["photos"] = [p for p in current_photos if p not in photos_to_delete]
                        print(f"Removed {len(photos_to_delete)} photos from property {id}")
                except json.JSONDecodeError:
                    print(f"Invalid JSON in photos_to_delete for property {id}")
            
        print(f"Edits saved for {id}: {prop}")
        return redirect(url_for("manage_listings"))
    except Exception as e:
        error_message = f"Error saving edits for {id}: {str(e)}"
        log_and_notify_error("Save Edit Error", error_message)
        return str(e), 500
@app.route("/upload-image/<uuid>", methods=["POST"])
@login_required
def upload_image(uuid):
    if "file" not in request.files:
        message = "No file part"
        log_and_notify_error("Upload Error", message)
        return jsonify(success=False, message=message), 400
    file = request.files["file"]
    if file.filename == "":
        message = "No selected file"
        log_and_notify_error("Upload Error", message)
        return jsonify(success=False, message=message), 400
    file_ext = os.path.splitext(file.filename)[1]
    new_filename = f"{uuid}_{secrets.token_hex(8)}{file_ext}"
    save_path = os.path.join(UPLOAD_FOLDER, new_filename)
    file.save(save_path)
    try:
        with Image.open(save_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            letterboxed = letterbox_to_16_9(im)
            letterboxed.save(save_path)
    except Exception as e:
        error_message = f"Failed to process uploaded image: {e}"
        log_and_notify_error("Image Processing Error", error_message)
    new_image_url = url_for("static", filename=f"uploads/{new_filename}", _external=False)
    return jsonify(success=True, new_image_url=new_image_url)
@app.route("/image-edit-notify", methods=["POST"])
@login_required
def image_edit_notify():
    try:
        data = request.json
        image_urls = data.get("images", [])
        notify_image_edit(image_urls)
        return jsonify(message="Notification sent."), 200
    except Exception as e:
        error_message = f"Failed to notify image edit: {str(e)}"
        log_and_notify_error("Image Edit Notification Error", error_message)
        return jsonify(message="Failed to send notification."), 500
if __name__ == "__main__":
    print("Warming property cache and processing images...")
    print_check_file(PROPERTY_APPTS_FILE, "Appointments file at startup")
    app.run("0.0.0.0", port=5000, debug=False)

