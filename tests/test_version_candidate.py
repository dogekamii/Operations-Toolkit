import json
from pathlib import Path
import unittest
import cnmaestro_speed_manager as toolkit

ROOT = Path(__file__).resolve().parents[1]


def version_tuple(value):
    return tuple(int(part) for part in str(value).split("."))


class VersionCandidateTests(unittest.TestCase):
    def test_candidate_version_is_wired_through_release_artifacts(self):
        self.assertEqual(toolkit.APP_VERSION, "1.6.1")
        self.assertIn("# Operations Toolkit 1.6.1", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("Operations-Toolkit-v1.6.1", (ROOT / "build_windows.bat").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        self.assertIn("Operations-Toolkit-v1.6.1", workflow)

    def test_every_test_module_runs_in_ci(self):
        workflow = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        for name in (
            "tests/test_credentials.py",
            "tests/test_mosaic_service.py",
            "tests/test_mosaic_credentials.py",
            "tests/test_mosaic_ui.py",
            "tests/test_association_service.py",
            "tests/test_association_ui.py",
            "tests/test_version_candidate.py",
            "tests/test_updater.py",
            "tests/test_inline_tool_views.py",
        ):
            self.assertIn(name, workflow)

    def test_update_manifest_never_advertises_a_future_version(self):
        # latest.json is published in a follow-up metadata commit once the
        # release asset exists, so a candidate may legitimately trail it.
        manifest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        self.assertLessEqual(version_tuple(manifest["version"]), version_tuple(toolkit.APP_VERSION))
        self.assertTrue(
            str(manifest["download_url"]).startswith(
                "https://github.com/dogekamii/Operations-Toolkit/releases/download/"
            )
        )

if __name__ == "__main__":
    unittest.main()
