#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import base64
import secrets
import smtplib
import logging
import requests
from io import BytesIO
from email.message import EmailMessage
from PIL import Image, ImageOps
from flask import Flask, jsonify, render_template_string, request, g, url_for
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask
app = Flask(__name__)

# Setup logging
LOG_FILE = "application.log"
logging.basicConfig(filename=LOG_FILE, level=logging.ERROR, format='%(asctime)s:%(levelname)s:%(message)s')

# Ensure 'static/uploads' folder exists for local file storage
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Memory Caches
properties_cache = []
photos_cache = {}

# Email Notification Setup
def send_email(subject, body):
    smtp_server = "smtp.gmail.com"
    port = 587
    sender_email = 'your-email@gmail.com'
    app_password = os.getenv('EMAIL_APP_PASSWORD')

    if not app_password:
        raise ValueError("No app password set in the environment")

    msg = EmailMessage()
    msg.set_content(body)
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = sender_email  # Send the email to yourself

    body += "\nView application logs here: http://localhost:5000/logs"
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp_server, port) as server:
            server.starttls()  # Secure the connection
            server.login(sender_email, app_password)
            server.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Failed to send email: {e}")

# Function to log errors and send email alerts
def log_and_notify_error(subject, error_message):
    logging.error(error_message)
    send_email(subject, error_message)

# Function to notify about edited images
def notify_image_edit(image_urls):
    subject = "Image Edited Notification"
    body = "The following image(s) have been edited:\n" + "\n".join(image_urls)
    send_email(subject, body)

# Request timing demonstration
@app.before_request
def before_request():
    g.start_time = time.time()

@app.after_request
def after_request(response):
    if hasattr(g, "start_time"):
        elapsed_time = time.time() - g.start_time
        if elapsed_time < 0.1:
            elapsed_ms = elapsed_time * 1000
            print(f"Time taken for {request.path}: {elapsed_ms:.2f}ms")
        else:
            print(f"Time taken for {request.path}: {elapsed_time:.2f}s")
    return response

# Functions to fetch and process property data
def letterbox_to_16_9(img: Image.Image) -> Image.Image:
    target_ratio = 16 / 9
    width, height = img.size
    if height == 0:
        return img
    original_ratio = width / height
    if abs(original_ratio - target_ratio) < 1e-5:
        return img
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

def fetch_properties():
    global properties_cache
    api_url = "https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/propertiesforrent"
    try:
        response = requests.get(api_url)
        response.raise_for_status()
        data = response.json()
        properties_cache = [fetch_property_details(uuid.strip()) for uuid in data.get("property_ids", [])]
    except requests.ConnectionError:
        log_and_notify_error("API Connection Error", "Failed to connect to properties API.")
    except requests.Timeout:
        log_and_notify_error("API Timeout Error", "Connection to properties API timed out.")
    except requests.RequestException as e:
        log_and_notify_error("API Request Error", f"General error occurred: {str(e)}")

def fetch_property_photos(uuid):
    photos_url = f"https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/{uuid}/photos"
    processed_photos = []
    try:
        response = requests.get(photos_url, timeout=5)
        response.raise_for_status()
        photo_urls = response.json() if response.ok else []
        for url in photo_urls:
            try:
                img_resp = requests.get(url, timeout=5)
                img_resp.raise_for_status()
                with Image.open(BytesIO(img_resp.content)) as img:
                    img_16_9 = letterbox_to_16_9(ImageOps.exif_transpose(img).convert("RGB"))
                    buffered = BytesIO()
                    img_16_9.save(buffered, format="JPEG")
                    encoded_img = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    processed_photos.append(f"data:image/jpeg;base64,{encoded_img}")
            except Exception as err:
                log_and_notify_error("Image Processing Error", f"Failed processing image {url}: {err}")
    except requests.RequestException as e:
        log_and_notify_error("API Photos Error", f"Failed fetching photos for {uuid}: {str(e)}")
    photos_cache[uuid] = processed_photos
    return processed_photos

def fetch_property_details(uuid):
    details_url = f"https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/{uuid}/details"
    try:
        response = requests.get(details_url)
        response.raise_for_status()
        property_info = response.json()
        if "included_utilities" in property_info:
            property_info["included_amenities"] = property_info.pop("included_utilities")
        property_info.setdefault("blurb", "This is a beautiful property in a great location.")
        property_info["photos"] = fetch_property_photos(uuid)
        return property_info
    except requests.RequestException as e:
        log_and_notify_error("API Property Details Error", f"Error fetching details for {uuid}: {str(e)}")
        return None

# Route to view logs
@app.route("/logs")
def view_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as log_file:
            log_content = log_file.read()
        return f"<pre>{log_content}</pre>"
    else:
        return "No logs available."

# Function to handle issue reports
@app.route("/report-issue", methods=["POST"])
def report_issue():
    user_name = request.form.get("name")
    issue_description = request.form.get("description")
    if not user_name or not issue_description:
        return "Name and description are required fields.", 400
        
    email_body = f"Issue reported by {user_name}:\n\n{issue_description}"
    send_email("User Reported Issue", email_body)
    return "Issue reported successfully!"

# HTML Base Template
base_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Somewheria LLC</title>
    <link href="https://unpkg.com/cropperjs/dist/cropper.min.css" rel="stylesheet" />
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #ffffff;
            color: #000000;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }
        header {
            background-color: #007BFF;
            color: white;
            text-align: center;
            padding: 15px 0;
        }
        nav ul {
            list-style-type: none;
            margin: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            padding-left: 0;
        }
        nav ul li {
            margin: 0 10px;
            position: relative;
        }
        nav ul li a {
            color: white;
            text-decoration: none;
            padding: 8px 16px;
            font-weight: bold;
            display: block;
            transition: transform 0.3s, box-shadow 0.3s, background-color 0.3s;
        }
        nav ul li a:hover {
            background-color: #0056b3;
            transform: scale(1.2) rotate(5deg);
            box-shadow: 0 0 20px rgba(0, 91, 187, 0.5), 0 0 10px rgba(0, 91, 187, 0.2) inset;
            border-radius: 5px;
        }
        nav ul li img {
            height: 40px;
            cursor: pointer;
        }
        main {
            flex: 1;
            margin: 20px auto;
            max-width: 900px;
        }
        .home-section img {
            width: 100%; 
            max-width: 500px; 
            height: auto;
        }
        .property-images img, .carousel-images img {
            width: 200px;
            height: 120px;
            object-fit: cover;
            margin: 5px 0;
            cursor: pointer;
            transition: transform 0.3s;
        }
        .property-images img:hover, .carousel-images img:hover {
            transform: scale(1.05);
        }
        .feedback-form {
            margin: 20px 0;
            border: 1px solid #ccc;
            padding: 10px;
            border-radius: 5px;
            background-color: #f8f8f8;
        }
        footer {
            border-top: 1px solid #ccc;
            text-align: center;
            padding: 10px 0;
            background-color: #f8f8f8;
        }
    </style>
</head>
<body>
    <header>
        <h1>Somewheria LLC</h1>
        <nav>
            <ul>
                <li><a href="/">Home</a></li>
                <li><a href="/for-rent">For Rent</a></li>
                <li><a href="/about">About Us</a></li>
                <li><a href="/contact">Contact Us</a></li>
                <li>
                    <img src="{{ url_for('static', filename='web_light_rd_SI@1x.png') }}" alt="Google Sign-In">
                </li>
            </ul>
        </nav>
    </header>
    <main>
        {% block content %}{% endblock %}
        <div class="feedback-form">
            <h3>Report an Issue</h3>
            <form action="/report-issue" method="post">
                <label for="name">Your Name:</label><br>
                <input type="text" id="name" name="name" required><br><br>
                <label for="description">Issue Description:</label><br>
                <textarea id="description" name="description" rows="5" placeholder="Describe the issue..." required></textarea>
                <br>
                <input type="submit" value="Submit Issue">
            </form>
        </div>
    </main>
    <footer>
        <p>&copy; 2025 Somewheria LLC. All Rights Reserved.</p>
    </footer>
    <script src="https://unpkg.com/cropperjs/dist/cropper.min.js"></script>
    <script>
        let cropper;
        let selectedImageElement = null;

        function toggleEditable(element) {
            element.contentEditable = element.contentEditable === "true" ? "false" : "true";
            const editBtn = element.querySelector('.edit-button');
            const saveBtn = element.querySelector('.save-button');
            if (element.contentEditable === "true") {
                editBtn.style.display = "none";
                saveBtn.style.display = "inline-block";
                element.focus();
            } else {
                editBtn.style.display = "inline-block";
                saveBtn.style.display = "none";
                notifyImageEdit([element.src]);
            }
        }

        function notifyImageEdit(imageUrls) {
            fetch("/image-edit-notify", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ images: imageUrls })
            }).then((res) => res.json()).then((data) => {
                console.log(data.message);
            });
        }

        function saveChanges(element, id = null) {
            element.contentEditable = "false";
            const editBtn = element.querySelector('.edit-button');
            const saveBtn = element.querySelector('.save-button');
            editBtn.style.display = "inline-block";
            saveBtn.style.display = "none";
            const content = element.innerHTML;
            if (id) {
                fetch(`/save-edit/${id}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content })
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        alert('Changes saved successfully!');
                    } else {
                        alert('Failed to save changes.');
                    }
                })
                .catch(err => alert('Error saving content: ' + err));
            }
        }

        function setupCarousels() {
            document.querySelectorAll('.image-carousel').forEach(carousel => {
                let currentIndex = 0;
                const images = carousel.querySelectorAll('.carousel-images img');
                const totalImages = images.length;
                const leftArrow = carousel.querySelector('.carousel-arrow.left');
                const rightArrow = carousel.querySelector('.carousel-arrow.right');
                
                function updateCarousel() {
                    images.forEach((img, idx) => {
                        img.style.display = (idx === currentIndex) ? 'block' : 'none';
                    });
                }
                
                if (totalImages > 0) {
                    updateCarousel();
                    leftArrow.addEventListener('click', () => {
                        currentIndex = (currentIndex === 0) ? totalImages - 1 : currentIndex - 1;
                        updateCarousel();
                    });
                    rightArrow.addEventListener('click', () => {
                        currentIndex = (currentIndex === totalImages - 1) ? 0 : currentIndex + 1;
                        updateCarousel();
                    });
                }
            });
        }

        function editImage(imageEl) {
            selectedImageElement = imageEl;
            const modal = document.createElement('div');
            modal.style.position = 'fixed';
            modal.style.top = 0; 
            modal.style.left = 0;
            modal.style.right = 0; 
            modal.style.bottom = 0;
            modal.style.backgroundColor = 'rgba(0, 0, 0, 0.5)';
            modal.style.display = 'flex';
            modal.style.justifyContent = 'center';
            modal.style.alignItems = 'center';
            modal.style.zIndex = 99999;
            const container = document.createElement('div');
            container.style.backgroundColor = '#fff';
            container.style.padding = '20px';
            container.style.position = 'relative';
            const cropImg = document.createElement('img');
            cropImg.src = imageEl.src;
            cropImg.id = "cropper-img";
            const saveBtn = document.createElement('button');
            saveBtn.innerText = 'Save';
            saveBtn.style.marginTop = '10px';
            saveBtn.onclick = () => {
                if (cropper) {
                    const canvas = cropper.getCroppedCanvas();
                    if (canvas) {
                        imageEl.src = canvas.toDataURL("image/jpeg");
                        notifyImageEdit([imageEl.src]);
                    }
                }
                document.body.removeChild(modal);
            };
            const cancelBtn = document.createElement('button');
            cancelBtn.innerText = 'Cancel';
            cancelBtn.style.marginTop = '10px';
            cancelBtn.style.marginLeft = '10px';
            cancelBtn.onclick = () => {
                document.body.removeChild(modal);
            };
            container.appendChild(cropImg);
            container.appendChild(document.createElement('br'));
            container.appendChild(saveBtn);
            container.appendChild(cancelBtn);
            modal.appendChild(container);
            document.body.appendChild(modal);
            cropper = new Cropper(cropImg, {
                aspectRatio: 16 / 9,
                viewMode: 1
            });
        }

        function addNewImage(uuid) {
            document.getElementById('add-image-input').click();
        }

        async function handleFileChange(e, uuid) {
            const file = e.target.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            try {
                const res = await fetch(`/upload-image/${uuid}`, {
                    method: 'POST',
                    body: formData
                });
                const data = await res.json();
                if (data.success) {
                    const imgContainer = document.getElementById('image-container');
                    if (imgContainer) {
                        const newImg = document.createElement('img');
                        newImg.src = data.new_image_url;
                        newImg.alt = 'Uploaded Image';
                        newImg.onclick = () => editImage(newImg);
                        imgContainer.appendChild(newImg);
                    }
                } else {
                    console.error(data.message || "Error uploading image.");
                }
            } catch (err) {
                console.error("Upload failed: ", err);
            }
        }

        function openGoogleMaps(address) {
            const encodedAddress = encodeURIComponent(address);
            const googleMapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodedAddress}`;
            window.open(googleMapsUrl, '_blank');
        }

        document.addEventListener('DOMContentLoaded', () => {
            setupCarousels();
            const addImgInput = document.getElementById('add-image-input');
            if (addImgInput) {
                addImgInput.addEventListener('change', (evt) => {
                    const uuid = addImgInput.getAttribute('data-uuid');
                    handleFileChange(evt, uuid);
                });
            }
        });
    </script>
</body>
</html>
"""

# Flask Routes
@app.route("/")
def landing_page():
    home_html = """
    {% block content %}
    <div class="home-section">
        <h2>Welcome to Somewheria LLC</h2>
        <p>Find your ideal rental property today!</p>
        <img src="{{ url_for('static', filename='rental_image.jpg') }}" alt="Rental Property">
    </div>
    {% endblock %}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", home_html)
    return render_template_string(final_html)

@app.route("/for-rent")
def for_rent():
    properties = properties_cache
    no_properties = not properties or len(properties) == 0
    error_message = "We're currently experiencing issues with our system. Please check back later." if no_properties else None
    rent_html = """
    {% block content %}
    <h2>Available Properties</h2>
    {% if properties %}
        <ul class="property-list">
            {% for property in properties %}
            <li>
                <div class="image-carousel">
                    <button class="carousel-arrow left" contenteditable="false"><</button>
                    <button class="carousel-arrow right" contenteditable="false">></button>
                    <div class="carousel-images">
                        {% for photo in property.photos %}
                        <img src="{{ photo }}" alt="Property Photo" onclick="editImage(this)">
                        {% endfor %}
                    </div>
                </div>
                <div class="editable" contenteditable="false" id="property-{{ property.id }}">
                    <button class="edit-button" contenteditable="false" 
                            onclick="toggleEditable(this.parentNode)">Edit</button>
                    <button class="save-button" contenteditable="false" 
                            onclick="saveChanges(this.parentNode, 'property-{{ property.id }}')">Save</button>
                    <strong>{{ property.name }}</strong><br>
                    <em>Bedrooms: {{ property.bedrooms }}</em><br>
                    <em>Bathrooms: {{ property.bathrooms }}</em><br>
                    <em>Rental Rate: ${{ property.rent }}</em><br>
                    <em>Square Footage: {{ property.sqft }} sqft</em><br>
                    <a href="/property/{{ property.id }}" contenteditable="false">View More Details</a> |
                    <a href="/property-images/{{ property.id }}" contenteditable="false">Edit Images</a>
                </div>
            </li>
            {% endfor %}
        </ul>
    {% else %}
        <p>{{ error_message }}</p>
    {% endif %}
    {% endblock %}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", rent_html)
    return render_template_string(final_html, properties=properties, error_message=error_message)

@app.route("/property/<uuid>")
def property_details(uuid):
    property_info = next((p for p in properties_cache if p and p.get("id") == uuid), None)
    if not property_info:
        return "Property not found", 404
    
    detail_html = """
    {% block content %}
    <h2>{{ property.name }}</h2>
    <div class="image-carousel">
        <button class="carousel-arrow left" contenteditable="false"><</button>
        <button class="carousel-arrow right" contenteditable="false">></button>
        <div class="carousel-images">
            {% for photo in property.photos %}
            <img src="{{ photo }}" alt="Property Photo" onclick="editImage(this)">
            {% endfor %}
        </div>
    </div>
    <div class="editable" contenteditable="false" id="property-{{ property.id }}">
        <button class="edit-button" contenteditable="false" 
                onclick="toggleEditable(this.parentNode)">Edit</button>
        <button class="save-button" contenteditable="false" 
                onclick="saveChanges(this.parentNode, 'property-{{ property.id }}')">Save</button>
        <p><strong>Address:</strong> {{ property.address }}
        <button onclick="openGoogleMaps('{{ property.address }}')" 
            style="margin-left: 10px; font-size: 0.9em; contenteditable='false';">View in Google Maps</button>
        </p>
        <p><strong>Bedrooms:</strong> {{ property.bedrooms }}</p>
        <p><strong>Bathrooms:</strong> {{ property.bathrooms }}</p>
        <p><strong>Square Footage:</strong> {{ property.sqft }} sqft</p>
        <p><strong>Deposit:</strong> ${{ property.deposit }}</p>
        <p><strong>Rental Rate:</strong> ${{ property.rent }}</p>
        <p><strong>Blurb About Property:</strong> {{ property.blurb }}</p>
        <p><strong>Included Amenities:</strong></p>
        <ul>
            {% for amenity in property.included_amenities %}
            <li>{{ amenity }}</li>
            {% endfor %}
        </ul>
        <p><strong>Pets Allowed:</strong> {{ property.pets_allowed }}</p>
        <a href="/property-images/{{ property.id }}" contenteditable="false">Edit Images</a>
    </div>
    {% endblock %}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", detail_html)
    return render_template_string(final_html, property=property_info)

@app.route("/property-images/<uuid>")
def property_images(uuid):
    property_info = next((p for p in properties_cache if p and p.get("id") == uuid), None)
    if not property_info:
        return "No images found", 404
    
    property_photos = property_info.get('photos', [])
    property_name = property_info.get('name', 'Property')
    
    images_html = f"""
    {{% block content %}}
    <h2>Manage Images for {property_name}</h2>
    <div id="image-container" class="property-images">
        {{% for photo in photos %}}
            <img src="{{{{ photo }}}}" alt="Property Image {{{{ loop.index }}}}" onclick="editImage(this)">
        {{% endfor %}}
    </div>
    <div class="action-buttons">
        <input 
            type="file" 
            id="add-image-input" 
            style="display:none;" 
            data-uuid="{{{{ uuid }}}}"
        >
        <button 
            class="edit-button" 
            data-type="upload" 
            contenteditable="false"
            onclick="addNewImage('{{{{ uuid }}}}');"
        >
            Add New Image
        </button>
    </div>
    {{% endblock %}}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", images_html)
    return render_template_string(final_html, uuid=uuid, photos=property_photos, property=property_info)

@app.route("/about")
def about_us():
    about_html = """
    {% block content %}
    <h2>About Somewheria LLC</h2>
    <div class="editable" contenteditable="false" id="about">
        <button class="edit-button" contenteditable="false" onclick="toggleEditable(this.parentNode)">Edit</button>
        <button class="save-button" contenteditable="false" onclick="saveChanges(this.parentNode, 'about')">Save</button>
        <p>Email: <a href="mailto:contact@somewheria.com">contact@somewheria.com</a></p>
        <p>Contact: Angela - Phone number: ###-###-#### (TBD)</p>
        <p>More about our company will be provided soon. Stay tuned!</p>
    </div>
    {% endblock %}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", about_html)
    return render_template_string(final_html)

@app.route("/contact")
def contact_us():
    contact_html = """
    {% block content %}
    <h2>Contact Us</h2>
    <p>If you have any questions, feel free to reach out via the form below.</p>
    <form action="mailto:contact@somewheria.com" method="POST" enctype="text/plain">
        <input type="text" placeholder="Your Name" required>
        <input type="email" placeholder="Your Email" required>
        <textarea placeholder="Your Message" rows="5" required></textarea>
        <input type="submit" value="Send Message">
    </form>
    {% endblock %}
    """
    final_html = base_html.replace("{% block content %}{% endblock %}", contact_html)
    return render_template_string(final_html)

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

# Main Application Start
if __name__ == "__main__":
    try:
        print("Fetching property data before starting the server...")
        fetch_properties()
        send_email("Server Started", "The server has started successfully and is running.")
        app.run(host="0.0.0.0", port=5000)
    except Exception as e:
        error_message = f"Server failed to start: {e}"
        log_and_notify_error("Server Start Failure", error_message)
