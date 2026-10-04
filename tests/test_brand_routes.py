"""New public downloads must not turn the portal into a filesystem server."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from machine_commerce.portal import create_portal


class BrandRoutes(unittest.TestCase):
    def test_only_named_brand_exports_are_public(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)
            brand = site / "assets/brand"
            brand.mkdir(parents=True)
            (brand / "skew-mark-ink.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            (brand / "private.json").write_text('{"private":true}')
            (site / "brand.html").write_text("Brand assets")
            with TestClient(create_portal(site)) as client:
                self.assertEqual(client.get("/brand.html").status_code, 200)
                response = client.get("/assets/brand/skew-mark-ink.svg")
                self.assertEqual(response.status_code, 200)
                self.assertIn("image/svg+xml", response.headers["content-type"])
                self.assertEqual(client.get("/assets/brand/private.json").status_code, 404)
                self.assertEqual(client.get("/assets/brand/skew-mark-white.png").status_code, 404)
                self.assertEqual(client.get("/assets/brand/%2e%2e/private.json").status_code, 404)
                self.assertEqual(client.get("/assets/app-mining.svg").status_code, 200)
                self.assertEqual(client.get("/assets/app-fuel.svg").status_code, 200)
