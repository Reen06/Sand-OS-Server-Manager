import sys
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from app import dockerhub_apps  # noqa: E402
from app.models import AppDef  # noqa: E402


def _manifest() -> dict:
    return dockerhub_apps._validate_manifest({
        "schema": 1,
        "id": "webcad",
        "label": "WebCAD/CAM old published label",
        "icon": "cpu",
        "color": "blue",
        "desc": "published copy",
        "kind": "web",
        "mode": "shared",
        "internal_port": 8137,
        "gpu": False,
        "multi_node": False,
        "mounts": [],
        "env_required": [],
    })


class DockerHubAppIdentityTest(unittest.TestCase):
    def test_published_builtin_keeps_one_canonical_identity(self):
        built_in = AppDef(
            id="webcad", label="WebCAD/CAM", icon="webcad", color="blue",
            desc="canonical", image="sm-webcad:dev", kind="web", mode="shared",
            internal_port=8137, multi_node=True, strict_ready=True,
            dockerhub_repo="reen16/webcad",
        )
        app = dockerhub_apps._manifest_to_appdef(
            _manifest(), "reen16/webcad:latest", {}, app_id="webcad", built_in=built_in,
        )

        self.assertEqual(app.id, "webcad")
        self.assertEqual(app.image, "reen16/webcad:latest")
        self.assertEqual(app.icon, "webcad")
        self.assertTrue(app.multi_node)
        self.assertTrue(app.strict_ready)
        self.assertEqual(app.dockerhub_repo, "reen16/webcad")

    def test_unknown_published_app_remains_namespaced(self):
        app = dockerhub_apps._manifest_to_appdef(_manifest(), "someone/webcad:latest", {})
        self.assertEqual(app.id, "hub-webcad")


if __name__ == "__main__":
    unittest.main()
