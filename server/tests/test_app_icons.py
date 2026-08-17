import ast
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "server" / "app" / "registry.py"
ICON_DIR = REPO / "server" / "app" / "appicons"
OWNER_APPROVED_ICON_EXCEPTIONS = {"ledger": "logs"}


def _catalog_identities() -> dict[str, str]:
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "CATALOG":
            continue
        identities = {}
        for key, value in zip(node.value.keys, node.value.values):
            if not isinstance(value, ast.Call):
                continue
            fields = {item.arg: item.value for item in value.keywords}
            app_id = ast.literal_eval(fields.get("id", key))
            identities[app_id] = ast.literal_eval(fields["icon"])
        return identities
    raise AssertionError("CATALOG was not found in registry.py")


class AppIconTest(unittest.TestCase):
    def test_catalog_apps_use_canonical_identity_except_owner_approved_fallbacks(self):
        identities = _catalog_identities()
        self.assertGreater(len(identities), 0)
        self.assertEqual(
            {app_id: icon for app_id, icon in identities.items() if icon != app_id},
            OWNER_APPROVED_ICON_EXCEPTIONS,
            "Non-canonical AppDef.icon values must be explicit owner-approved exceptions",
        )

    def test_every_catalog_app_has_a_valid_svg_for_pwa_and_ios_rendering(self):
        for app_id in _catalog_identities():
            if app_id in OWNER_APPROVED_ICON_EXCEPTIONS:
                continue
            with self.subTest(app=app_id):
                path = ICON_DIR / f"{app_id}.svg"
                self.assertTrue(path.is_file(), f"missing {path.relative_to(REPO)}")
                root = ET.parse(path).getroot()
                self.assertTrue(root.tag.endswith("svg"))
                self.assertIn("viewBox", root.attrib)

    def test_owner_approved_fallbacks_do_not_ship_a_canonical_asset(self):
        for app_id in OWNER_APPROVED_ICON_EXCEPTIONS:
            with self.subTest(app=app_id):
                self.assertFalse((ICON_DIR / f"{app_id}.svg").exists())


if __name__ == "__main__":
    unittest.main()
