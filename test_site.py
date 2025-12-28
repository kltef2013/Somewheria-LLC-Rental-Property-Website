import pytest
from playwright.sync_api import Page, expect

def test_homepage_loads(page: Page):
    # This assumes your Flask app is running on port 5000 in GitHub Actions
    page.goto("http://127.0.0.1:5000")
    
    # Check if the title is correct (Change this to match your site)
    expect(page).to_have_title("Somewheria LLC")

def test_admin_404(page: Page):
    # Check that a non-existent page actually returns a 404
    response = page.goto("http://127.0.0.1:5000/this-page-does-not-exist")
    assert response.status == 404
