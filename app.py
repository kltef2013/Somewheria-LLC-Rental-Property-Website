from flask import Flask, render_template_string, url_for, request, g
import requests
import time

app = Flask(__name__)

# Global storage for cached properties
properties_cache = []

@app.before_request
def before_request():
    g.start_time = time.time()

@app.after_request
def after_request(response):
    if hasattr(g, 'start_time'):
        elapsed_time = time.time() - g.start_time
        if elapsed_time < 0.1:
            elapsed_milliseconds = elapsed_time * 1000
            print(f"Time taken for {request.path}: {elapsed_milliseconds:.2f}ms")
        else:
            print(f"Time taken for {request.path}: {elapsed_time:.2f}s")
    return response

def fetch_properties():
    """
    Fetches a list of properties from the API and loads their details
    and images into memory.
    """
    global properties_cache
    first_api_url = "https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/propertiesforrent"
    properties_list = []
    try:
        response = requests.get(first_api_url)
        response.raise_for_status()
        uuids = response.json()
        
        for uuid in uuids['property_ids']:
            property_details = fetch_property_details(uuid.strip())
            if property_details:
                properties_list.append(property_details)
    except requests.exceptions.RequestException as e:
        print(f"Request error occurred: {e}")
    
    properties_cache = properties_list

def fetch_property_photos(uuid):
    """
    Fetches photos for a property using its UUID.
    """
    photos_url = f"https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/{uuid}/photos"
    try:
        response = requests.get(photos_url)
        response.raise_for_status()
        return response.json() if response.ok else []
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch photos for UUID {uuid}: {e}")
        return []

def fetch_property_details(uuid):
    """
    Fetches detailed information for a property using its UUID.
    """
    second_api_url = f"https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/{uuid}/details"
    try:
        response = requests.get(second_api_url)
        response.raise_for_status()
        property_details = response.json()
        
        if 'included_utilities' in property_details:
            property_details['included_amenities'] = property_details.pop('included_utilities')
        
        property_details.setdefault('blurb', "This is a beautiful property in a great location.")
        
        # Fetch and pre-render photos
        property_details['photos'] = fetch_property_photos(uuid)
        return property_details
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch details for UUID {uuid}: {e}")
        return None

base_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Somewheria LLC</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #ffffff;
            color: #000000;
            transition: background-color 0.3s, color 0.3s;
        }
        header {
            background-color: #007BFF;
            padding: 10px 0;
            text-align: center;
            color: #ffffff;
        }
        nav {
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 0 15px;
        }
        nav ul {
            list-style-type: none;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            flex-wrap: wrap;
        }
        nav ul li {
            margin: 0 10px;
        }
        nav ul li a {
            color: white;
            text-decoration: none;
            padding: 8px 12px;
            transition: background-color 0.3s ease, transform 0.3s ease;
            display: inline-block;
        }
        nav ul li a:hover {
            background-color: #0056b3;
            transform: scale(1.1);
        }
        main {
            padding: 20px;
            max-width: 900px;
            margin: 0 auto;
        }
        ul.property-list {
            list-style-type: none;
            padding: 0;
        }
        ul.property-list li {
            display: flex;
            align-items: flex-start;
            margin: 20px 0;
            text-align: left;
        }
        ul.property-list li div:nth-of-type(2) {
            margin-left: 20px;
            flex: 1;
        }
        .image-carousel {
            display: flex;
            align-items: center;
            position: relative;
            width: 300px;
            height: 200px;
            overflow: hidden;
            margin: 0;
        }
        .carousel-images img {
            width: 100%;
            height: auto;
            max-height: 200px;
            display: none;
        }
        .carousel-arrow {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background-color: rgba(0, 0, 0, 0.5);
            color: white;
            border: none;
            padding: 10px;
            cursor: pointer;
        }
        .carousel-arrow.left {
            left: 10px;
        }
        .carousel-arrow.right {
            right: 10px;
        }
        .navigate-button {
            background-color: #007BFF;
            color: white;
            border: none;
            border-radius: 5px;
            padding: 10px 15px;
            cursor: pointer;
            text-decoration: none;
            margin-left: 25px;
        }
        form input, form textarea {
            width: 100%;
            padding: 10px;
            margin: 10px 0;
            border: 1px solid #ccc;
            border-radius: 4px;
        }
        .home-section {
            display: flex;
            flex-direction: column;
            align-items: center;
            max-width: 90%;
            margin: auto;
        }
        .home-section img {
            max-width: 100%;
            height: auto;
            margin-top: 10px;
        }
        .google-signin-btn {
            display: block;
            margin: 20px auto;
            cursor: pointer;
            max-width: 200px;
        }
        /* Responsive Design */
        @media (max-width: 768px) {
            .home-section, main {
                max-width: 95%;
                margin: auto;
            }
            nav ul {
                flex-direction: column;
                align-items: center;
            }
            ul.property-list li {
                flex-direction: column;
                align-items: center;
            }
            ul.property-list li div:nth-of-type(2) {
                margin-left: 0;
            }
            .image-carousel {
                width: 100%;
                margin-bottom: 10px;
            }
        }
        /* Dark mode styles */
        @media (prefers-color-scheme: dark) {
            body {
                background-color: #121212;
                color: #ffffff;
            }
            header {
                background-color: #1f1f1f;
            }
            nav ul li a {
                color: #ffffff;
            }
            nav ul li a:hover {
                background-color: #333333;
            }
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
            </ul>
        </nav>
    </header>
    <main>
        {% block content %}{% endblock %}
    </main>
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const carousels = document.querySelectorAll('.image-carousel');
            carousels.forEach(carousel => {
                let currentIndex = 0;
                const images = carousel.querySelectorAll('img');
                const totalImages = images.length;
                const updateCarousel = () => {
                    images.forEach((img, index) => {
                        img.style.display = index === currentIndex ? 'block' : 'none';
                    });
                };
                carousel.querySelector('.carousel-arrow.left').addEventListener('click', () => {
                    currentIndex = (currentIndex === 0) ? totalImages - 1 : currentIndex - 1;
                    updateCarousel();
                });
                carousel.querySelector('.carousel-arrow.right').addEventListener('click', () => {
                    currentIndex = (currentIndex === totalImages - 1) ? 0 : currentIndex + 1;
                    updateCarousel();
                });
                updateCarousel();
            });
        });
    </script>
</body>
</html>
"""

@app.route('/')
def landing_page():
    home_content = """
    {% block content %}
    <div class="home-section">
        <h2>Welcome to Somewheria LLC</h2>
        <p>Find your ideal rental property today!</p>
        <img src="{{ url_for('static', filename='rental_image.jpg') }}" alt="Rental Property">
    </div>
    {% endblock %}
    """
    return render_template_string(
        base_html.replace("{% block content %}{% endblock %}", home_content)
    )

@app.route('/for-rent')
def for_rent():
    """
    The 'For Rent' page shows properties from the global cache.
    """
    properties = properties_cache
    error_message = None
    no_properties = (properties is None) or (len(properties) == 0)
    if no_properties:
        error_message = "We're currently experiencing issues with our system, please check back later."
    rent_content = """
    {% block content %}
    <h2>Available Properties</h2>
    {% if properties %}
        <ul class="property-list">
            {% for property in properties %}
            <li>
                <div class="image-carousel">
                    <button class="carousel-arrow left"><</button>
                    <button class="carousel-arrow right">></button>
                    <div class="carousel-images">
                        {% for photo in property.photos %}
                        <img src="{{ photo }}" alt="Property Photo">
                        {% endfor %}
                    </div>
                </div>
                <div>
                    <strong>{{ property.name }}</strong><br>
                    <em>Bedrooms: {{ property.bedrooms }}</em><br>
                    <em>Bathrooms: {{ property.bathrooms }}</em><br>
                    <em>Rental Rate: ${{ property.rent }}</em><br>
                    <em>Square Footage: {{ property.sqft }} sqft</em><br>
                    <a href="/property/{{ property.id }}">View More Details</a>
                </div>
            </li>
            {% endfor %}
        </ul>
    {% else %}
        <p>{{ error_message }}</p>
    {% endif %}
    {% endblock %}
    """
    return render_template_string(
        base_html.replace("{% block content %}{% endblock %}", rent_content),
        properties=properties,
        error_message=error_message
    )

@app.route('/property/<uuid>')
def property_details(uuid):
    """
    Renders a separate, nested detail page for each property.
    """
    property_info = next((p for p in properties_cache if p['id'] == uuid), None)
    if not property_info:
        return "Property not found", 404
    property_content = """
    {% block content %}
    <h2>{{ property.name }}</h2>
    <div class="image-carousel">
        <button class="carousel-arrow left"><</button>
        <button class="carousel-arrow right">></button>
        <div class="carousel-images">
            {% for photo in property.photos %}
            <img src="{{ photo }}" alt="Property Photo">
            {% endfor %}
        </div>
    </div>
    <p><strong>Address:</strong> {{ property.address }}</p>
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
    {% endblock %}
    """
    return render_template_string(
        base_html.replace("{% block content %}{% endblock %}", property_content),
        property=property_info
    )

@app.route('/about')
def about_us():
    about_content = """
    {% block content %}
    <h2>About Somewheria LLC</h2>
    <p>Email: <a href="mailto:contact@somewheria.com">contact@somewheria.com</a></p>
    <p>Contact: Angela - Phone number: ###-###-#### (TBD)</p>
    <p>More about our company will be provided soon. Stay tuned!</p>
    {% endblock %}
    """
    return render_template_string(
        base_html.replace("{% block content %}{% endblock %}", about_content)
    )

@app.route('/contact')
def contact_us():
    contact_content = """
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
    return render_template_string(
        base_html.replace("{% block content %}{% endblock %}", contact_content)
    )

if __name__ == '__main__':
    print("Starting application and fetching all property data...")
    fetch_properties()  # Pre-fetch properties when the app starts
    app.run(host='0.0.0.0', port=5000, debug=True)