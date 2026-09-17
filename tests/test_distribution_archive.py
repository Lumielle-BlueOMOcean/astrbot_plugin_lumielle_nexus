import io
import subprocess
import tarfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RUNTIME_FILES = {
    "main.py",
    "core.py",
    "storage.py",
    "qq_adapter.py",
    "exporter.py",
    "poll_service.py",
    "poll_web.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "logo.png",
    "README.md",
    "LICENSE",
}


def archive_files() -> list[str]:
    result = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--worktree-attributes",
            "HEAD",
        ],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        return sorted(
            member.name
            for member in archive.getmembers()
            if member.isfile()
        )


class DistributionArchiveTests(unittest.TestCase):
    def test_marketplace_archive_keeps_runtime_files_and_short_paths(self):
        files = set(archive_files())

        self.assertTrue(REQUIRED_RUNTIME_FILES <= files)
        self.assertNotIn("AGENTS.md", files)
        self.assertFalse(any(path.startswith("docs/") for path in files))
        self.assertFalse(any(path.startswith("tests/") for path in files))

        longest_relative_path = max(files, key=len)
        github_archive_root = (
            "Lumielle-BlueOMOcean-astrbot_plugin_lumielle_nexus-"
            + "0" * 40
        )
        install_prefix = (
            r"C:\Users\ExampleUser\Desktop\AstrBot\data\plugins"
            r"\astrbot_plugin_lumielle_nexus"
        )
        final_path = (
            install_prefix
            + "\\"
            + github_archive_root
            + "\\"
            + longest_relative_path.replace("/", "\\")
        )

        self.assertLess(
            len(final_path),
            240,
            f"archive path is too close to MAX_PATH: {final_path}",
        )


if __name__ == "__main__":
    unittest.main()
