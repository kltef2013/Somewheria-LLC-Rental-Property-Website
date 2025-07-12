# Somewheria LLC Rental Property Listing Web Application

Welcome to the Somewheria LLC Rental Property Listing web application, designed to showcase rental properties and provide a seamless user experience for individuals searching for their next home.

This web application is built using Python's Flask framework and serves as a front-end interface for users to explore various rental properties, view detailed information, and get in contact with Somewheria LLC for queries.

## Features

- **Home Page**: Overview of the service and company with a welcoming message.
- **Properties for Rent**: Displays all available rental properties with details such as name, bedrooms, bathrooms, rental rate, and square footage.
- **Property Details Page**: A dedicated page for each property with comprehensive information including images, address, amenities, and contact information.
- **About Us**: Details about Somewheria LLC, including contact information.
- **Contact Us**: A simple contact form for users to reach out with their queries.

## Getting Started

### Prerequisites

To run this application, you need:

- Python 3.x installed on your system.
- `pip` package manager for Python.
- `Flask` package installed. You can install it using:
  ```bash
  pip install Flask
  ```
- `requests` package installed for API calls:
  ```bash
  pip install requests
  ```

### Running the Application

To run the application, use the following command:

```bash
python3 app.py
```

The application will start on `localhost:5000`, and you can access it by navigating to `http://localhost:5000` in your web browser.

### Application Structure

- **App Initialization**: The application is initialized with Flask and starts by pre-fetching properties to improve performance.
- **Routing**:
  - `/` - Displays the home page.
  - `/for-rent` - Lists available properties for rent.
  - `/property/<uuid>` - Displays detailed information for a specific property.
  - `/about` - Shows information about Somewheria LLC.
  - `/contact` - Presents a contact form for user inquiries.

- **Templates**: The application uses the `render_template_string` function to dynamically generate HTML content served to the users.

- **CSS and HTML**: Seamlessly integrated for responsive design and aesthetic appeal, with a focus on user experience and mobile responsiveness.

- **Performance Logging**: The application logs the time taken to respond to each request in milliseconds or seconds for performance monitoring.

## Branching Strategy

### Main Branch

The `stable` branch contains the stable version of the application, ready for production use. It is thoroughly tested and validated to ensure a smooth user experience.

### Developer Branch

The `dev` branch is used for ongoing development and experimentation. This branch is considered unstable and may contain features or fixes that are still in progress. Use this branch with caution.

## API Integration

The application fetches property data using the following API endpoints:

1. `https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/propertiesforrent`: Fetches the list of property IDs.
2. `https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/<uuid>/details`: Fetches detailed property information.
3. `https://7pdnexz05a.execute-api.us-east-1.amazonaws.com/test/properties/<uuid>/photos`: Fetches property photos using the UUID.

---

Thank you for using the Somewheria LLC Rental Property Listing web application. We hope you have a pleasant experience exploring our properties.
