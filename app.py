import os
import time
import base64
import secrets
import logging
import requests
import smtplib
from io import BytesIO
from PIL import Image, ImageOps
from email.message import EmailMessage
from flask import Flask, jsonify, render_template_string, request, g, url_for
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
@app.route("/report-issue-complete")
def report_issue_complete():
    issue_form_html = """
    {% block content %}
    <div class='px-4 sm:px-8 md:px-16 lg:px-24 py-10 max-w-full sm:max-w-2xl mx-auto'>
      <h2 class='text-2xl font-bold mb-6'>Report an Issue</h2>
      <form action='/report-issue' method='post' class='max-w-lg mx-auto'>
        <input type='text' name='name' placeholder='Your Name' required class='w-full p-2 mb-3 border rounded'>
        <textarea name='description' rows='5' placeholder='Describe the issue, request, or question...' required class='w-full p-2 mb-3 border rounded'></textarea>
        <button type='submit' class='bg-blue-500 text-white py-2 px-4 rounded hover:bg-blue-600 w-full'>Submit</button>
      </form>
    </div>
    {% endblock %}
    """
    return render_template_string(SHELL.replace("{% block content %}{% endblock %}", issue_form_html), title="Report an Issue")
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
    }
    #hamburger-icon.open .mobile-menu {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
    }
    .mobile-menu li {
      margin-bottom: 10px;
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
  </style>
</head>
<body style='font-family:"Plus Jakarta Sans","Noto Sans",sans-serif;'>
<div class="relative flex min-h-screen flex-col group/design-root overflow-x-hidden">
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
        <h2 class="text-[#111518] dark:text-white text-lg font-bold leading-tight tracking-[-0.015em]">Somewheria, LLC.</h2>
      </a>
      <nav class="desktop-nav flex-1 hidden sm:block" aria-label="Main navigation">
        <ul class="flex items-center gap-6 justify-center w-full">
          <li><a href="{{ url_for('home') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 px-3 py-2">Home</a></li>
          <li><a href="{{ url_for('for_rent') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 px-3 py-2">For Rent</a></li>
          <li><a href="{{ url_for('about') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 px-3 py-2">About Us</a></li>
          <li><a href="{{ url_for('contact') }}" class="text-[#111518] dark:text-white text-sm font-medium leading-normal transition hover:bg-[#EAEDF1] dark:hover:bg-neutral-800 hover:rounded hover:text-blue-600 px-3 py-2">Contact Us</a></li>
        </ul>
      </nav>
      <div class="flex flex-1 justify-end items-center">
        <div id="hamburger-icon" onclick="toggleMobileMenu(this)" aria-label="Open menu" tabindex="0" style="margin-left:auto;">
          <div class="bar1"></div>
          <div class="bar2"></div>
          <div class="bar3"></div>
          <ul class="mobile-menu dark:bg-neutral-900 dark:text-white">
            <li><a href="{{ url_for('home') }}" class="dark:text-white">Home</a></li>
            <li><a href="{{ url_for('for_rent') }}" class="dark:text-white">For Rent</a></li>
            <li><a href="{{ url_for('about') }}" class="dark:text-white">About Us</a></li>
            <li><a href="{{ url_for('contact') }}" class="dark:text-white">Contact Us</a></li>
          </ul>
        </div>
      </div>
    </header>
    <script>
      function toggleMobileMenu(menu) {
        menu.classList.toggle('open');
      }
    </script>
    <main class="flex-1 dark:bg-neutral-950 dark:text-white">{% block content %}{% endblock %}</main>
   <footer class="py-5 text-center bg-gray-100 dark:bg-neutral-900">
  <p class="text-gray-600 dark:text-gray-300 mb-2">&copy; 2024/25 Somewheria, LLC. All Rights Reserved.</p>
  <div>
      <a href="{{ url_for('report_issue_complete') }}" class="text-blue-600 dark:text-blue-400 underline text-sm">Report an issue</a>
  </div>
</footer>
<script>
// Auto-detect system dark mode and apply Tailwind dark class
if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
  document.documentElement.classList.add('dark');
} else {
  document.documentElement.classList.remove('dark');
}
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
  if (e.matches) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
});
</script>
</div>
</body>
</html>
'''
import datetime
property_appointments = load_appointments()
@app.route('/')
def home():
    try:
        return render_template_string(SHELL.replace(
            "{% block content %}{% endblock %}",
            """
            {% block content %}
<div class="px-4 sm:px-8 md:px-16 lg:px-24 py-6 sm:py-10 md:py-12 max-w-full sm:max-w-2xl mx-auto">
                <h1 class="text-3xl font-bold mb-4">Welcome to Somewheria, LLC.</h1>
                <p class="mb-6">Find your next rental in style and comfort.</p>
                <a class="bg-blue-600 hover:bg-blue-800 text-white px-6 py-3 rounded" href="{{ url_for('for_rent') }}">
                    View Properties
                </a>
            </div>
            {% endblock %}
            """
        ), title="Home")
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
    rent_html = """
    {% block content %}
<div class="px-4 sm:px-8 md:px-16 lg:px-24 py-6 sm:py-10 md:py-12">
        <h2 class="text-2xl font-bold mb-8">Available Properties</h2>
        {% if not properties %}
            <div class="text-center text-gray-500 py-10">Loading properties, please try again in a moment.</div>
        {% else %}
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
        {% for prop in properties %}
            <div class="rounded shadow p-4 bg-white">
                <a href="{{ url_for('property_details', uuid=prop.id) }}">
                    <img src="{{ prop.thumbnail }}" alt="Property Image" class="rounded mb-3 w-full h-48 object-cover" />
                    <div class="font-bold text-lg">{{ prop.name }}</div>
                    <div class="text-gray-600">{{ prop.address }}</div>
                    <div class="mt-2 text-blue-800 font-semibold">${{ prop.rent }}/mo</div>
                </a>
                <div class="mt-3">
                    <a href="{{ url_for('property_details', uuid=prop.id) }}"
                    class="underline text-blue-600">Details</a>
                </div>
            </div>
        {% endfor %}
        </div>
        {% endif %}
    </div>
    {% endblock %}
    <script>
    // Property change detection logic
    (function() {
      const STORAGE_KEY = "property_snapshot";
      // Helper: deep compare two objects (shallow for arrays of objects)
      function diffProperties(oldList, newList) {
        const oldMap = {};
        oldList.forEach(p => oldMap[p.id] = p);
        const newMap = {};
        newList.forEach(p => newMap[p.id] = p);
        const changes = [];
        for (const id in newMap) {
          if (!oldMap[id]) {
            changes.push({ id, type: "added", new: newMap[id] });
            continue;
          }
          const diffs = [];
          for (const key of Object.keys(newMap[id])) {
            if (typeof newMap[id][key] === "object") continue; // skip nested
            if (oldMap[id][key] !== newMap[id][key]) {
              diffs.push({
                field: key,
                old: oldMap[id][key],
                new: newMap[id][key]
              });
            }
          }
          if (diffs.length) {
            changes.push({ id, type: "changed", diffs });
          }
        }
        for (const id in oldMap) {
          if (!newMap[id]) {
            changes.push({ id, type: "removed", old: oldMap[id] });
          }
        }
        return changes;
      }
      // Fetch latest properties from API
      fetch("/for-rent.json")
        .then(resp => resp.json())
        .then(newProps => {
          let oldProps = [];
          try {
            oldProps = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
          } catch {}
          const changes = diffProperties(oldProps, newProps);
          if (changes.length === 0) {
            console.log("[Property Check] No changes detected in property data.");
          } else {
            console.group("[Property Check] Changes detected in property data:");
            changes.forEach(change => {
              if (change.type === "added") {
                console.log(`+ Property added: ID ${change.id}`, change.new);
              } else if (change.type === "removed") {
                console.log(`- Property removed: ID ${change.id}`, change.old);
              } else if (change.type === "changed") {
                console.group(`~ Property changed: ID ${change.id}`);
                console.table(change.diffs.map(d => ({
                  Field: d.field,
                  "Old Value": d.old,
                  "New Value": d.new
                })));
                console.groupEnd();
              }
            });
            console.groupEnd();
          }
          // Save new snapshot
          localStorage.setItem(STORAGE_KEY, JSON.stringify(newProps));
        })
        .catch(e => {
          console.warn("[Property Check] Could not fetch property data for change detection.", e);
        });
    })();
    </script>
    """
    return render_template_string(SHELL.replace("{% block content %}{% endblock %}", rent_html),
            properties=properties, title="For Rent"
    )

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
    # Use cache for property details
    with cache_lock:
        property_info = next((p for p in properties_cache if p.get("id") == uuid), None)
    if not property_info:
        return "Property not found", 404
    global property_appointments
    property_appointments = load_appointments()
    booked_dates = sorted(list(property_appointments.get(uuid, set())))
    nowdate = datetime.date.today().strftime("%Y-%m-%d")
    detail_html = """
    {% block content %}
<div class="px-4 sm:px-8 md:px-16 lg:px-24 flex flex-col items-center py-7">
      <div class="max-w-[960px] w-full relative">
        <div class="bg-white rounded-xl mb-5 relative">
          <!-- Carousel -->
          <div class="relative flex justify-center items-center" style="height: 360px; overflow:hidden;">
            {% set image_count = property.photos | length %}
            <img 
              id="carouselImg" 
              src="{{ property.thumbnail }}" 
              alt="Property Image" 
              class="rounded object-cover w-full h-80 transition-all duration-300"
              style="max-height: 340px;"
            />
            <button id="carouselPrev" class="absolute left-1 top-1/2 -translate-y-1/2 px-2 py-2 rounded-full bg-white/70 hover:bg-blue-100" style="z-index:15;">
              &#8249;
            </button>
            <button id="carouselNext" class="absolute right-1 top-1/2 -translate-y-1/2 px-2 py-2 rounded-full bg-white/70 hover:bg-blue-100" style="z-index:15;">
              &#8250;
            </button>
            <div class="absolute bottom-2 right-5 flex gap-1">
              {% for idx in range(image_count) %}
                <span class="block w-2 h-2 rounded-full {% if idx==0 %}bg-blue-500{% else %}bg-gray-300{% endif %} carousel-dot" data-idx="{{ idx }}"></span>
              {% endfor %}
            </div>
          </div>
        </div>
        <!-- Main content -->
        <h1 class="text-[#111518] text-[22px] font-bold tracking-tight px-4 pb-3 pt-5">{{ property.name }}</h1>
        <p class="text-[#111518] text-base pb-3 pt-1 px-4">{{ property.blurb }}</p>
        <h2 class="text-[#111518] text-[22px] font-bold px-4 pb-3 pt-5">Property Details</h2>
        <div class="p-4 grid grid-cols-2">
          <div class="flex flex-col gap-1 border-t py-4 pr-2"><p class="text-[#60768a] text-sm">Address</p><p class="text-[#111518] text-sm">{{ property.address }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pl-2"><p class="text-[#60768a] text-sm">Rent</p><p class="text-[#111518] text-sm">${{ property.rent }}/month</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pr-2"><p class="text-[#60768a] text-sm">Deposit</p><p class="text-[#111518] text-sm">${{ property.deposit }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pl-2"><p class="text-[#60768a] text-sm">Square Footage</p><p class="text-[#111518] text-sm">{{ property.sqft }} sq ft</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pr-2"><p class="text-[#60768a] text-sm">Bedrooms</p><p class="text-[#111518] text-sm">{{ property.bedrooms }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pl-2"><p class="text-[#60768a] text-sm">Bathrooms</p><p class="text-[#111518] text-sm">{{ property.bathrooms }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pr-2"><p class="text-[#60768a] text-sm">Lease Term</p><p class="text-[#111518] text-sm">{{ property.lease_length }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pl-2"><p class="text-[#60768a] text-sm">Amenities</p><p class="text-[#111518] text-sm">{{ property.included_amenities|join(', ') }}</p></div>
          <div class="flex flex-col gap-1 border-t py-4 pr-2"><p class="text-[#60768a] text-sm">Pets Allowed</p>
            <p class="text-[#111518] text-sm">
              {% if property.pets_allowed is defined %}
                {{ property.pets_allowed }}
              {% else %}
                Unknown
              {% endif %}
            </p>
          </div>
        </div>
        <h2 class="text-[#111518] text-[22px] font-bold px-4 pb-3 pt-5">Description</h2>
        <p class="text-[#111518] text-base pb-3 pt-1 px-4">{{ property.description or property.blurb }}</p>
        <!-- Request Appointment Button -->
        <div class="px-4 pt-6 flex justify-end">
          <button id="openCalModal" class="bg-blue-600 text-white py-2 px-4 rounded hover:bg-blue-700 font-bold shadow">
            Request Appointment
          </button>
        </div>
        <!-- Calendar Modal -->
        <div id="calModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50 hidden">
          <div class="bg-white rounded p-6 w-full max-w-[370px] shadow">
            <div class="flex justify-between items-center mb-5">
              <div class="text-lg font-bold">Request an Appointment</div>
              <button onclick="closeCalModal()" class="bg-gray-200 rounded px-2 py-1 text-lg leading-none">&times;</button>
            </div>
            <form id="apptForm">
              <label for="apptName" class="font-semibold text-sm">Your Name:</label>
              <input id="apptName" type="text" required class="w-full border rounded mb-3 p-2"/>
              <label class="font-semibold text-sm block mb-2">Choose Date:</label>
              <input id="apptDate" name="date" type="date" min="{{ nowdate }}" required class="mb-3 border rounded p-2 w-full"/>
              <label for="contactMethod" class="font-semibold text-sm block mb-2">Preferred Contact Method:</label>
              <select id="contactMethod" name="contactMethod" class="w-full border rounded mb-3 p-2">
                <option value="email">Email</option>
                <option value="text">Text</option>
              </select>
              <div id="contactInfoContainer">
                <label for="contactInfo" class="font-semibold text-sm block mb-2">Your Email:</label>
                <input id="contactInfo" type="email" name="contactInfo" required class="w-full border rounded mb-3 p-2"/>
              </div>
              <div id="apptDateFeedback" class="text-red-600 text-xs mb-2"></div>
              <button type="submit" class="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 w-full">Request Booking</button>
              <div id="apptSubmitFeedback" class="text-green-700 text-xs mt-2"></div>
            </form>
          </div>
        </div>
      </div>
    </div>
    <script>
    // Carousel
    document.addEventListener("DOMContentLoaded", function() {
      let images = {{ property.photos | tojson }};
      let idx = 0;
      let imgTag = document.getElementById('carouselImg');
      let dots = document.querySelectorAll('.carousel-dot');
      function updateCarousel(index) {
        idx = index;
        imgTag.src = images[idx];
        dots.forEach((d,i) => d.className = 'block w-2 h-2 rounded-full carousel-dot ' + (i === idx ? 'bg-blue-500' : 'bg-gray-300'));
      }
      document.getElementById('carouselPrev').onclick = function() {
        idx = (idx - 1 + images.length) % images.length;
        updateCarousel(idx);
      };
      document.getElementById('carouselNext').onclick = function() {
        idx = (idx + 1) % images.length;
        updateCarousel(idx);
      };
      dots.forEach((dot, i) => dot.onclick = () => updateCarousel(i));
      updateCarousel(0);
      // Modal calendar logic
      let modal = document.getElementById('calModal');
      let openBtn = document.getElementById('openCalModal');
      let closeCalModal = window.closeCalModal = () => modal.classList.add("hidden");
      openBtn.onclick = function() {
        modal.classList.remove("hidden");
        document.getElementById('apptSubmitFeedback').innerHTML = '';
      };
      let minDate = "{{ nowdate }}";
      // Contact method logic
      const contactMethod = document.getElementById('contactMethod');
      const contactInfoContainer = document.getElementById('contactInfoContainer');
      const contactInfoInput = document.getElementById('contactInfo');
      function updateContactInfoField() {
        if (contactMethod.value === "text") {
          contactInfoContainer.innerHTML = `<label for="contactInfo" class="font-semibold text-sm block mb-2">Your Phone Number:</label>
            <input id="contactInfo" type="tel" name="contactInfo" required class="w-full border rounded mb-3 p-2"/>`;
        } else {
          contactInfoContainer.innerHTML = `<label for="contactInfo" class="font-semibold text-sm block mb-2">Your Email:</label>
            <input id="contactInfo" type="email" name="contactInfo" required class="w-full border rounded mb-3 p-2"/>`;
        }
      }
      contactMethod.addEventListener("change", updateContactInfoField);
      updateContactInfoField();
      // Form handler
      document.getElementById('apptForm').onsubmit = async function(ev){
        ev.preventDefault();
        let name = document.getElementById('apptName').value.trim();
        let date = document.getElementById('apptDate').value;
        let method = document.getElementById('contactMethod').value;
        let contactInfo = document.getElementById('contactInfo').value.trim();
        let feedbackBox = document.getElementById('apptDateFeedback');
        if (!name || !date || !contactInfo) return;
        feedbackBox.textContent = '';
        let resp = await fetch('{{ url_for("schedule_appointment", uuid=property.id) }}', {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name:name, date:date, contact_method:method, contact_info:contactInfo})
        });
        let data = await resp.json();
        if (data.success){
          document.getElementById('apptSubmitFeedback').innerHTML = "Your appointment was requested!";
          document.getElementById('apptForm').reset();
        } else {
          feedbackBox.textContent = data.error || "Failed to request.";
        }
      }
    });
    </script>
    <script>
    // Property change detection for details page (with image diff)
    (function() {
      const STORAGE_KEY = "property_snapshot";
      // Helper: find property by id in list
      function findById(list, id) {
        return (list || []).find(p => p.id === id);
      }
      // Helper: diff two arrays (for images)
      function diffArrays(oldArr, newArr) {
        oldArr = oldArr || [];
        newArr = newArr || [];
        const added = newArr.filter(x => !oldArr.includes(x));
        const removed = oldArr.filter(x => !newArr.includes(x));
        const unchanged = newArr.filter(x => oldArr.includes(x));
        return { added, removed, unchanged };
      }
      // Helper: diff two property objects (fields + images)
      function diffProperty(oldProp, newProp) {
        if (!oldProp) return { added: true, new: newProp };
        const diffs = [];
        // Shallow fields (non-object, except images)
        for (const key of Object.keys(newProp)) {
          if (key === "photos" || key === "thumbnail") continue;
          if (typeof newProp[key] === "object") continue;
          if (oldProp[key] !== newProp[key]) {
            diffs.push({
              field: key,
              old: oldProp[key],
              new: newProp[key]
            });
          }
        }
        // Thumbnail diff
        if (oldProp.thumbnail !== newProp.thumbnail) {
          diffs.push({
            field: "thumbnail",
            old: oldProp.thumbnail,
            new: newProp.thumbnail
          });
        }
        // Photos array diff
        const photoDiff = diffArrays(oldProp.photos, newProp.photos);
        if (photoDiff.added.length || photoDiff.removed.length) {
          diffs.push({
            field: "photos",
            added: photoDiff.added,
            removed: photoDiff.removed
          });
        }
        return diffs.length ? { changed: true, diffs } : null;
      }
      // Get current property id and data
      const propId = "{{ property.id }}";
      const currentProp = {{ property | tojson }};
      let oldProps = [];
      try {
        oldProps = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      } catch {}
      const oldProp = findById(oldProps, propId);
      const diff = diffProperty(oldProp, currentProp);
      if (!oldProp) {
        console.log(`[Property Details] This property was not in the previous snapshot (new or first visit).`);
      } else if (!diff) {
        console.log(`[Property Details] No changes detected for this property.`);
      } else if (diff.changed) {
        console.group(`[Property Details] Changes detected for property ID ${propId}:`);
        diff.diffs.forEach(d => {
          if (d.field === "photos") {
            if (d.added.length)
              console.log("Photos added:", d.added);
            if (d.removed.length)
              console.log("Photos removed:", d.removed);
            if (!d.added.length && !d.removed.length)
              console.log("No changes in photos.");
          } else if (d.field === "thumbnail") {
            console.log("Thumbnail changed:", { old: d.old, new: d.new });
          } else {
            console.log(`Field changed: ${d.field}`, { old: d.old, new: d.new });
          }
        });
        // Also print a summary table for non-image fields
        const tableRows = diff.diffs
          .filter(d => d.field !== "photos" && d.field !== "thumbnail")
          .map(d => ({
            Field: d.field,
            "Old Value": d.old,
            "New Value": d.new
          }));
        if (tableRows.length) {
          console.table(tableRows);
        }
        console.groupEnd();
      } else if (diff.added) {
        console.log(`[Property Details] This property was just added.`, diff.new);
      }
    })();
    </script>
    {% endblock %}
    """
    return render_template_string(
        SHELL.replace("{% block content %}{% endblock %}", detail_html),
        property=property_info,
        nowdate=nowdate,
        booked_dates=booked_dates,
        title=property_info.get("name", "Property"),
    )
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
    about_html = """
    {% block content %}
    <div class="px-4 sm:px-8 md:px-16 lg:px-24 py-6 sm:py-10 md:py-12 max-w-full sm:max-w-2xl mx-auto">
        <h2 class="text-2xl font-bold mb-4">About Somewheria, LLC.</h2>
        <p>Email: <a href="mailto:angela@somewheria.com" class="underline text-blue-600">angela@somewheria.com</a></p>
        <p class="mt-1">Contact Person: Angela </p><p>Phone: (570) 236-9960</p>
        <p class="mt-5">We are a next-generation rental company, matching you to your ideal home seamlessly!</p>
    </div>
    {% endblock %}
    """
    return render_template_string(SHELL.replace("{% block content %}{% endblock %}", about_html), title="About")
@app.route("/contact")
def contact():
    contact_html = """
    {% block content %}
        <div class="px-4 sm:px-8 md:px-16 lg:px-24 py-6 sm:py-10 md:py-12 max-w-full sm:max-w-2xl mx-auto">
        <h2 class="text-2xl font-bold mb-4">Contact Us</h2>
        <form action="mailto:contact@somewheria.com" method="POST" enctype="text/plain" class="mt-4 max-w-md">
            <input type="text" placeholder="Your Name" required class="w-full p-2 mb-3 border rounded">
            <input type="email" placeholder="Your Email" required class="w-full p-2 mb-3 border rounded">
            <textarea placeholder="Your Message" rows="5" required class="w-full p-2 mb-3 border rounded"></textarea>
            <input type="submit" value="Send Message" class="mt-2 bg-blue-500 text-white px-4 py-2 rounded cursor-pointer hover:bg-blue-600">
        </form>
    </div>
    {% endblock %}
    """
    return render_template_string(SHELL.replace("{% block content %}{% endblock %}", contact_html), title="Contact")
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
                    # Only for 0-59
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
                    # Example: '2025-07-12 20\t59\t53,152'
                    match = re.match(r'(\d{4})-(\d{2})-(\d{2})[\s\t]+(\d{2})[\t:]+(\d{2})[\t:]+([\d,]+)', ts)
                    if match:
                        year, month, day, hour, minute, second = match.groups()
                        months = ['January','February','March','April','May','June','July','August','September','October','November','December']
                        month_word = months[int(month)-1] if month.isdigit() and 1 <= int(month) <= 12 else month
                        hour_word = number_to_words(hour)
                        minute_word = number_to_words(minute)
                        # For seconds, keep as is if it has comma (milliseconds)
                        second_main = second.split(',')[0]
                        second_word = number_to_words(second_main)
                        ms = ''
                        if ',' in second:
                            ms = ',' + second.split(',')[1]
                        return f"{month_word} {int(day)}, {year} at {hour_word} {minute_word} {second_word}{ms}"
                    return ts
                # Remove ANSI escape codes from message for readability
                import re
                ansi_escape = re.compile(r'\x1B\[[0-9;]*[mK]')
                clean_message = ansi_escape.sub('', message)
                log_entries.append({
                    'timestamp': timestamp_to_words(timestamp),
                    'level': level,
                    'message': clean_message
                })
    log_html = '''
    {% block content %}
    <div class="px-4 sm:px-8 md:px-16 lg:px-24 py-10 max-w-full sm:max-w-3xl mx-auto">
      <h2 class="text-2xl font-bold mb-6">Application Logs</h2>
      <div class="mb-4 flex items-center gap-3">
        <input id="logSearch" type="text" placeholder="Search logs..." class="border rounded p-2 w-full max-w-xs" />
        <button onclick="location.reload()" class="bg-blue-500 text-white px-3 py-2 rounded hover:bg-blue-600">Refresh</button>
      </div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-sm border rounded shadow">
          <thead class="bg-gray-100">
            <tr>
              <th class="px-3 py-2 text-left">Timestamp</th>
              <th class="px-3 py-2 text-left">Level</th>
              <th class="px-3 py-2 text-left">Message</th>
            </tr>
          </thead>
          <tbody id="logTableBody">
            {% for entry in log_entries %}
            <tr class="{% if entry.level.strip() == 'ERROR' %}bg-red-50{% elif entry.level.strip() == 'WARNING' %}bg-yellow-50{% elif entry.level.strip() == 'INFO' %}bg-blue-50{% endif %}">
              <td class="px-3 py-2 whitespace-nowrap">{{ entry.timestamp }}</td>
              <td class="px-3 py-2">
                <span class="px-2 py-1 rounded text-xs font-bold {% if entry.level.strip() == 'ERROR' %}bg-red-500 text-white{% elif entry.level.strip() == 'WARNING' %}bg-yellow-400 text-black{% elif entry.level.strip() == 'INFO' %}bg-blue-400 text-white{% else %}bg-gray-300 text-black{% endif %}">{{ entry.level }}</span>
              </td>
              <td class="px-3 py-2">{{ entry.message }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% if not log_entries %}
          <div class="text-gray-500 py-8 text-center">No logs available.</div>
        {% endif %}
      </div>
    </div>
    <script>
    // Simple client-side search
    document.getElementById('logSearch').addEventListener('input', function() {
      var filter = this.value.toLowerCase();
      var rows = document.querySelectorAll('#logTableBody tr');
      rows.forEach(function(row) {
        var text = row.textContent.toLowerCase();
        row.style.display = text.includes(filter) ? '' : 'none';
      });
    });
    </script>
    {% endblock %}
    '''
    return render_template_string(SHELL.replace('{% block content %}{% endblock %}', log_html), log_entries=log_entries, title="Logger")
@app.route("/report-issue", methods=["POST"])
def report_issue():
    user_name = request.form.get("name")
    issue_description = request.form.get("description")
    if not user_name or not issue_description:
        return "Name and description are required fields.", 400
    email_body = f"Issue reported by {user_name}:\n\n{issue_description}"
    send_email("User Reported Issue", email_body)
    return render_template_string(SHELL.replace(
        "{% block content %}{% endblock %}",
        """
        {% block content %}
        <div class="px-4 sm:px-8 md:px-16 lg:px-24 py-6 sm:py-10 md:py-12 max-w-full sm:max-w-2xl mx-auto">
            <div class="bg-green-100 text-green-800 font-bold px-6 py-5 rounded mb-8">
                Thank you {{ name }}, your report has been submitted!
            </div>
            <div class="text-gray-800">
                <div><strong>Submitted Description:</strong></div>
                <div class="bg-gray-50 border px-4 py-2 rounded mt-2 mb-4">{{ desc }}</div>
            </div>
            <a class="text-blue-600 underline" href="{{ url_for('home') }}">Back to home</a>
        </div>
        {% endblock %}
        """), name=user_name, desc=issue_description)
@app.route("/save-edit/<id>", methods=["POST"])
def save_edit(id):
    try:
        content = request.json.get("content", "")
        print(f"Saving edits for {id}: {content}")
        return jsonify(success=True, data=content)
    except Exception as e:
        error_message = f"Error saving edits for {id}: {str(e)}"
        log_and_notify_error("Save Edit Error", error_message)
        return jsonify(success=False, error=str(e)), 500
@app.route("/upload-image/<uuid>", methods=["POST"])
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
    app.run("0.0.0.0", port=80, debug=False)
